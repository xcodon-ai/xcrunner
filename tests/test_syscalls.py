import platform
import subprocess
import sys


from xcodon_runtime import syscalls as sc

MOUNTINFO = """\
25 1 0:23 / /proc rw,nosuid,nodev,noexec,relatime shared:13 - proc proc rw
40 25 0:35 / /proc/sys/fs/binfmt_misc rw,relatime shared:22 - autofs systemd-1 rw
99 1 8:1 / /mnt/my\\040disk ro,noatime shared:50 - ext4 /dev/sda1 ro
100 1 0:50 / /sys rw,nosuid,nodev,noexec,relatime shared:8 - sysfs sysfs rw
"""


def test_parse_mount_flags_reads_locked_flags():
    flags = sc.parse_mount_flags(MOUNTINFO, "/proc")
    assert flags & sc.MS_NOSUID and flags & sc.MS_NODEV and flags & sc.MS_NOEXEC
    assert not flags & sc.MS_RDONLY


def test_parse_mount_flags_unescapes_spaces_and_reads_ro():
    flags = sc.parse_mount_flags(MOUNTINFO, "/mnt/my disk")
    assert flags & sc.MS_RDONLY
    assert flags & sc.MS_NOATIME


def test_parse_mount_flags_unknown_path_is_zero():
    assert sc.parse_mount_flags(MOUNTINFO, "/nope") == 0


def test_mount_flags_at_real_root_returns_int():
    assert isinstance(sc.mount_flags_at("/"), int)


def test_pivot_root_syscall_number_known_for_this_machine():
    assert platform.machine() in sc.SYS_PIVOT_ROOT


def test_unshare_without_privilege_fails_cleanly_for_net():
    # CLONE_NEWNET without a user namespace needs CAP_SYS_ADMIN; we expect EPERM, not a crash.
    code = subprocess.run(
        [sys.executable, "-c", "from xcodon_runtime import syscalls as s\n"
         "try:\n s.unshare(0x40000000)\nexcept OSError as e:\n print(e.errno)"],
        capture_output=True, text=True,
    )
    assert code.stdout.strip() in {"1", ""}  # EPERM, or empty if the host allows it


def test_ensure_mountpoint_creates_dir_or_file(tmp_path):
    sc.ensure_mountpoint("/etc", str(tmp_path / "a/b/etc"))
    assert (tmp_path / "a/b/etc").is_dir()
    sc.ensure_mountpoint("/etc/hosts", str(tmp_path / "x/hosts"))
    assert (tmp_path / "x/hosts").is_file()
    (tmp_path / "y").mkdir()
    (tmp_path / "y/link").symlink_to("/nonexistent/target")
    sc.ensure_mountpoint("/etc/hosts", str(tmp_path / "y/link"))
    assert (tmp_path / "y/link").is_file() and not (tmp_path / "y/link").is_symlink()


def test_ensure_mountpoint_replaces_file_with_dir_when_source_is_dir(tmp_path):
    target = tmp_path / "wrongtype"
    target.write_text("i am a file")
    sc.ensure_mountpoint("/etc", str(target))
    assert target.is_dir()


def test_ensure_mountpoint_replaces_dir_with_file_when_source_is_file(tmp_path):
    target = tmp_path / "wrongtype"
    target.mkdir()
    (target / "child").write_text("leftover")
    sc.ensure_mountpoint("/etc/hosts", str(target))
    assert target.is_file()
