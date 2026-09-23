"""The keeper's mount plan as data. No namespaces needed, so this always runs."""

from xcodon_runtime import keeper


MOUNT_TARGET_AT = {"tmpfs": 1, "devpts": 1, "shm": 1, "proc": 1, "bind": 2}


def test_mount_plan_is_ordered_data(tmp_path):
    """The keeper's sandbox build is a list of steps, checkable without namespaces."""
    old_name = "xcodon-oldroot-plan"
    plan = {
        "lower": "/img/rootfs", "upper": str(tmp_path / "upper"), "work": str(tmp_path / "work"),
        "merged": str(tmp_path / "merged"), "uid": 0, "gid": 0, "hostname": "abc",
        "workdir": "/workspace",
        "binds": [{"source": "/host/data", "target": "/data", "readonly": False},
                  {"source": "/host/ro", "target": "/ro", "readonly": True}],
    }
    steps = keeper.build_mount_steps(plan, old_name)
    ops = [s[0] for s in steps]

    assert steps[0] == ("pivot_root", plan["merged"], f"{plan['merged']}/{old_name}")
    assert "pivot_root" not in ops[1:], "the pivot happens once, before every mount"
    mounts = [s for s in steps if s[0] in MOUNT_TARGET_AT]
    assert ops.index("umount_old_root") > ops.index(mounts[-1][0])
    assert steps[-1] == ("umount_old_root", f"/{old_name}")

    for step in mounts:
        target = step[MOUNT_TARGET_AT[step[0]]]
        assert target.startswith("/"), step
        assert not target.startswith(f"/{old_name}"), step
    for step in steps:
        if step[0] == "bind":
            assert step[1].startswith(f"/{old_name}/"), step

    assert ops.index("bind") < ops.index("proc"), "device binds come before /proc"
    user_binds = [i for i, s in enumerate(steps) if s[0] == "bind" and s[2] in ("/data", "/ro")]
    hosts = [i for i, s in enumerate(steps) if s[0] == "bind" and s[2] == "/etc/hosts"]
    assert hosts and min(user_binds) > hosts[0], "user binds come after /etc/hosts"
    assert steps[user_binds[0]] == ("bind", f"/{old_name}/host/data", "/data", False)
    assert steps[user_binds[1]] == ("bind", f"/{old_name}/host/ro", "/ro", True)
    assert ("mkdir", "/workspace") in steps
