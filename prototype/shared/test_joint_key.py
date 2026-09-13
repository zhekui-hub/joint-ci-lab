import unittest
from joint_key import (
    make_joint_key,
    merge_remote_tests,
    per_pr_required,
    hash_merged_tests,
    test_config_hash,
    workflow_key,
)


class JointKeyTests(unittest.TestCase):
    def test_same_versions_same_key(self):
        v = {"driver": "aaa", "synapse": "bbb", "sim": "ccc"}
        self.assertEqual(
            make_joint_key(v, "img:1", "cfg"),
            make_joint_key(v, "img:1", "cfg"),
        )

    def test_sha_change_changes_key(self):
        a = make_joint_key({"driver": "aaa", "synapse": "bbb"}, "img", "cfg")
        b = make_joint_key({"driver": "aaa", "synapse": "BBB"}, "img", "cfg")
        self.assertNotEqual(a, b)

    def test_merge_dedupes_identical(self):
        reports = [
            {
                "repo": "driver",
                "pr_number": 1,
                "remote_tests": [
                    {"id": "multirepo_runtest", "params": {"profile": "default"}},
                    {"id": "pseudo", "params": {"cards": 16}, "exclusive_to": "driver"},
                ],
            },
            {
                "repo": "synapse",
                "pr_number": 2,
                "remote_tests": [
                    {"id": "multirepo_runtest", "params": {"profile": "default"}},
                    {"id": "pseudo", "params": {"cards": 4}},
                ],
            },
        ]
        merged = merge_remote_tests(reports)
        ids = sorted((m["id"], m["params"].get("cards")) for m in merged)
        self.assertEqual(
            ids,
            [
                ("multirepo_runtest", None),
                ("pseudo", 4),
                ("pseudo", 16),
            ],
        )
        multi = next(m for m in merged if m["id"] == "multirepo_runtest")
        self.assertEqual(len(multi["consumers"]), 2)

    def test_per_pr_required(self):
        report = {
            "repo": "synapse",
            "pr_number": 2,
            "remote_tests": [{"id": "multirepo_runtest", "params": {"profile": "default"}}],
        }
        merged = merge_remote_tests([
            report,
            {
                "repo": "driver",
                "pr_number": 1,
                "remote_tests": [
                    {"id": "multirepo_runtest", "params": {"profile": "default"}},
                    {"id": "pseudo", "params": {"cards": 16}},
                ],
            },
        ])
        req = per_pr_required(report, merged)
        self.assertEqual(len(req), 1)
        self.assertTrue(req[0].startswith("multirepo_runtest:"))

    def test_hash_merged_tests_stable_and_content_based(self):
        a = [
            {"id": "multirepo_runtest", "params": {"profile": "default"}, "env": {}},
            {"id": "pseudo", "params": {"cards": 4}, "env": {}},
        ]
        b = list(reversed(a))  # order should not matter
        self.assertEqual(hash_merged_tests(a), hash_merged_tests(b))
        self.assertEqual(hash_merged_tests(a), test_config_hash(a))
        c = a + [{"id": "extra", "params": {}, "env": {}}]
        self.assertNotEqual(hash_merged_tests(a), hash_merged_tests(c))
        # Not based on len alone: different content same length → different hash
        d = [
            {"id": "multirepo_runtest", "params": {"profile": "other"}, "env": {}},
            {"id": "pseudo", "params": {"cards": 4}, "env": {}},
        ]
        self.assertNotEqual(hash_merged_tests(a), hash_merged_tests(d))

    def test_workflow_key(self):
        self.assertEqual(
            workflow_key({"id": "pseudo", "params": {"cards": 16}}),
            'pseudo:{"cards":16}',
        )


if __name__ == "__main__":
    unittest.main()
