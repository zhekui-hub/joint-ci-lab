import unittest
from joint_key import make_joint_key, merge_remote_tests, per_pr_required


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


if __name__ == "__main__":
    unittest.main()
