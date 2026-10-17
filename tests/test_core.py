import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dota_settings_copier as dsc  # noqa: E402

LOGINUSERS = """"users"
{
\t"76561198000000001"
\t{
\t\t"AccountName"\t\t"alice_login"
\t\t"PersonaName"\t\t"Alice"
\t}
\t"76561198000000002"
\t{
\t\t"AccountName"\t\t"bob_login"
\t\t"PersonaName"\t\t"Bob"
\t}
}
"""

FILES = [
    "remote/user_keys.vcfg",
    "remote/user_convars.vcfg",
    "remote/cfg/dotakeys_personal.lst",
    "remote/cfg/chat.cfg",
    "remote/cfg/herobuilds.cfg",
    "remote/scripts/control_groups.txt",
    "remote/guides/lich.build",
    "local/cfg/user_keys_0_slot0.vcfg",
    "local/cfg/user_keys_0_slot1.vcfg",
    "local/cfg/user_convars_0_slot0.vcfg",
    "local/cfg/video.txt",
    "local/cfg/machine_convars.vcfg",
]
# Account-specific data that must never be copied.
PRIVATE = ["remote/cfg/stats.dat", "remotecache.vdf", "remote/favorite_friends.json"]


def make_account(steam, account_id, tag):
    base = steam / "userdata" / account_id / "570"
    for rel in FILES + PRIVATE:
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{tag}:{rel}")
    return base


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.steam = Path(self.tmp.name) / "Steam"
        (self.steam / "config").mkdir(parents=True)
        (self.steam / "config" / "loginusers.vdf").write_text(LOGINUSERS)
        self.alice = make_account(self.steam, "39734273", "alice")
        self.bob = make_account(self.steam, "39734274", "bob")
        (self.steam / "userdata" / "0").mkdir()
        self.backups = Path(self.tmp.name) / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def accounts(self):
        return {a.name: a for a in dsc.get_accounts(self.steam)}

    def test_finds_accounts_with_names(self):
        accts = self.accounts()
        self.assertEqual(set(accts), {"Alice (alice_login)", "Bob (bob_login)"})
        self.assertEqual(accts["Alice (alice_login)"].path, self.alice)

    def test_copy_all_categories(self):
        accts = self.accounts()
        src, dst = accts["Alice (alice_login)"], accts["Bob (bob_login)"]
        result = dsc.copy_settings(src, dst, list(dsc.CATEGORIES), self.backups, log=lambda _m: None)

        self.assertEqual(result.failed, 0)
        self.assertEqual(result.copied, len(FILES))
        for rel in FILES:
            self.assertEqual((self.bob / rel).read_text(), f"alice:{rel}", rel)
        for rel in PRIVATE:
            self.assertEqual((self.bob / rel).read_text(), f"bob:{rel}", rel)
        # Backup holds Bob's original settings.
        self.assertEqual((result.backup / "remote/user_keys.vcfg").read_text(), "bob:remote/user_keys.vcfg")

    def test_copy_only_hotkeys(self):
        accts = self.accounts()
        src, dst = accts["Alice (alice_login)"], accts["Bob (bob_login)"]
        hotkeys = next(c for c in dsc.CATEGORIES if c.startswith("Hotkeys"))
        dsc.copy_settings(src, dst, [hotkeys], self.backups, log=lambda _m: None)
        self.assertEqual((self.bob / "remote/cfg/dotakeys_personal.lst").read_text(), "alice:remote/cfg/dotakeys_personal.lst")
        self.assertEqual((self.bob / "local/cfg/video.txt").read_text(), "bob:local/cfg/video.txt")

    def test_restore_backup(self):
        accts = self.accounts()
        src, dst = accts["Alice (alice_login)"], accts["Bob (bob_login)"]
        result = dsc.copy_settings(src, dst, list(dsc.CATEGORIES), self.backups, log=lambda _m: None)
        self.assertEqual(dsc.backup_account_id(result.backup), dst.id)
        dsc.restore_backup(result.backup, dst)
        for rel in FILES:
            self.assertEqual((self.bob / rel).read_text(), f"bob:{rel}", rel)

    def test_same_account_rejected(self):
        a = self.accounts()["Alice (alice_login)"]
        with self.assertRaises(ValueError):
            dsc.copy_settings(a, a, list(dsc.CATEGORIES), self.backups, log=lambda _m: None)


if __name__ == "__main__":
    unittest.main()
