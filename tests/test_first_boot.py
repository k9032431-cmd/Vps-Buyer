"""First-boot scripts really run: root password set, password logins on, ufw off, keys installed.

Each provider's script is run with bash against stub commands (chpasswd, chage, ufw, systemctl…)
and a temporary /etc/ssh, so a typo like an unbalanced bracket can't silently break the password."""
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from passlib.hash import sha512_crypt

from bot.providers import base, digitalocean, hostvds

PW = "Abc123xyz9"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAID6LbRUzalQL49DX5sqqfgDxxf0Ce0EetDiMpoHpXkuC me@laptop"


def run(script: str) -> dict:
    tmp = Path(tempfile.mkdtemp())
    bin_, ssh, root = tmp / "bin", tmp / "ssh", tmp / "root"
    bin_.mkdir(), ssh.mkdir(), root.mkdir()
    (ssh / "sshd_config").write_text("#PermitRootLogin prohibit-password\nPasswordAuthentication no\n")
    (ssh / "sshd_config.d").mkdir()
    (ssh / "sshd_config.d" / "50-cloud-init.conf").write_text("PasswordAuthentication no\n")
    log = tmp / "calls.log"
    for cmd in ("chpasswd", "chage", "passwd", "ufw", "systemctl", "service", "sleep"):
        f = bin_ / cmd
        f.write_text(f'#!/bin/bash\necho "{cmd} $*" >> {log}\n' + (f"cat >> {log}\necho >> {log}\n" if cmd == "chpasswd" else ""))
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    script = script.replace("/etc/ssh", str(ssh)).replace("/root/.ssh", str(root / ".ssh"))
    (tmp / "s.sh").write_text(script)
    check = subprocess.run(["bash", "-n", str(tmp / "s.sh")], capture_output=True, text=True)
    assert check.returncode == 0, f"syntax error: {check.stderr}\n{script}"
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    subprocess.run(["bash", str(tmp / "s.sh")], env=env, check=True, timeout=20)
    subprocess.run(["bash", "-c", "wait; sleep 0"], env=env)  # background parts finish fast with stub sleep
    for _ in range(100):  # the script's "( … ) &" parts run detached
        if log.exists() and "systemctl restart" in log.read_text():
            break
        subprocess.run(["/bin/sleep", "0.05"])
    calls = log.read_text() if log.exists() else ""
    dropin = ssh / "sshd_config.d" / "00-vps-buyer.conf"
    keys = root / ".ssh" / "authorized_keys"
    return {"calls": calls, "dropin": dropin.read_text() if dropin.exists() else "",
            "main": (ssh / "sshd_config").read_text(), "cloud": (ssh / "sshd_config.d" / "50-cloud-init.conf").read_text(),
            "keys": keys.read_text() if keys.exists() else ""}


def password_set(calls: str) -> bool:
    line = next((l for l in calls.splitlines() if l.startswith("root:$6$")), "")
    return bool(line) and sha512_crypt.verify(PW, line.split(":", 1)[1])


def check_password_server(r: dict, name: str):
    assert password_set(r["calls"]), f"{name}: root password not set\n{r['calls']}"
    assert "ufw disable" in r["calls"], f"{name}: ufw not switched off"
    assert "PasswordAuthentication yes" in r["dropin"] and "PermitRootLogin yes" in r["dropin"], name
    assert "PasswordAuthentication yes" in r["main"] and "PermitRootLogin yes" in r["main"], name
    assert "PasswordAuthentication yes" in r["cloud"], f"{name}: cloud-init still turns passwords off"
    assert "systemctl restart ssh" in r["calls"], f"{name}: sshd not restarted"


def test_first_boot():
    # DigitalOcean (password + the bot's key, as the bot orders it) — this one had an unbalanced bracket
    do = base.merge_scripts(digitalocean.password_script(PW), digitalocean.key_script([KEY]))
    r = run(do)
    check_password_server(r, "DigitalOcean")
    assert "chage -d" in r["calls"], "DigitalOcean: forced password change lifted"
    assert KEY in r["keys"]

    # HostVDS / Hetzner share one password script
    r = run(base.merge_scripts(hostvds.password_script(PW), base.ssh_key_script([KEY])))
    check_password_server(r, "HostVDS/Hetzner")
    assert KEY in r["keys"]

    # Vultr / Civo: their own password; the script opens access (password mode) …
    r = run(base.merge_scripts(base.open_access_script(True), base.ssh_key_script([KEY])))
    assert "ufw disable" in r["calls"] and "PasswordAuthentication yes" in r["dropin"] and KEY in r["keys"]
    # … and with your own key only the firewall goes, password logins stay as the image has them
    r = run(base.open_access_script(False))
    assert "ufw disable" in r["calls"] and not r["dropin"] and "PasswordAuthentication no" in r["main"]


if __name__ == "__main__":
    test_first_boot()
    print("OK")
