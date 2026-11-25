"""Tencent Cloud CVM + EIP against a fake API 3.0 (signed requests checked): IP first, server built
without a public IP and the EIP bound once it runs; regions, tariffs, images, cleanup."""
import asyncio
import base64
import itertools
import json

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import buyer
from bot.buyer import PurchaseJob, run_purchase
from bot.providers import PROVIDERS, RESERVE_PROVIDERS, SCOUT_PROVIDERS, make_client, tencent

ORIG_SLEEP = asyncio.sleep
SECRET = "AKIDtest123456 secretKEY987"
BOT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBotBotBotBotBotBotBotBotBotBotBotBotBot vps-buyer-bot"


def fake_tencent(eip_ips):
    eip_ips = itertools.cycle(eip_ips)
    st = {"eips": {}, "released": [], "instances": {}, "run": [], "terminated": [], "sg": [], "rules": [],
          "assoc": [], "polls": {}}
    app = web.Application()

    async def handle(req):
        service = req.match_info["service"]
        payload = await req.text()
        h = req.headers
        host = f"{service}.tencentcloudapi.com"
        want = tencent.sign("AKIDtest123456", "secretKEY987", service, host, payload, int(h["X-TC-Timestamp"]))
        if h["Authorization"] != want or h["X-TC-Version"] != "2017-03-12":
            return web.json_response({"Response": {"Error": {"Code": "AuthFailure.SignatureFailure",
                                                             "Message": "bad signature"}}})
        action, region, p = h["X-TC-Action"], h.get("X-TC-Region", ""), json.loads(payload or "{}")
        r = {"RequestId": "x"}
        if action == "DescribeRegions":
            r["RegionSet"] = [{"Region": "eu-frankfurt", "RegionName": "Europe (Frankfurt)", "RegionState": "AVAILABLE"},
                              {"Region": "ap-singapore", "RegionName": "Singapore", "RegionState": "AVAILABLE"},
                              {"Region": "eu-moscow", "RegionName": "Moscow", "RegionState": "UNAVAILABLE"}]
        elif action == "DescribeZoneInstanceConfigInfos":
            assert p["Filters"][0] == {"Name": "instance-charge-type", "Values": ["POSTPAID_BY_HOUR"]}
            r["InstanceTypeQuotaSet"] = [
                {"Zone": "eu-frankfurt-1", "InstanceType": "S5.MEDIUM2", "Cpu": 2, "Memory": 2, "Status": "SELL",
                 "Price": {"UnitPrice": 0.05, "ChargeUnit": "HOUR", "UnitPriceDiscount": 0.04}},
                {"Zone": "eu-frankfurt-2", "InstanceType": "S5.SMALL1", "Cpu": 1, "Memory": 1, "Status": "SELL",
                 "Price": {"UnitPrice": 0.02, "ChargeUnit": "HOUR", "UnitPriceDiscount": 0.02}},
                {"Zone": "eu-frankfurt-1", "InstanceType": "S5.SMALL1", "Cpu": 1, "Memory": 1, "Status": "SELL",
                 "Price": {"UnitPrice": 0.03, "ChargeUnit": "HOUR", "UnitPriceDiscount": 0.03}},
                {"Zone": "eu-frankfurt-1", "InstanceType": "SA2.MEDIUM4", "Cpu": 2, "Memory": 4,
                 "Status": "SOLD_OUT", "Price": {"UnitPrice": 0.01}}]
        elif action == "DescribeImages":
            r["ImageSet"] = [{"ImageId": "img-ubuntu", "OsName": "Ubuntu Server 22.04 LTS 64bit", "Platform": "Ubuntu"},
                             {"ImageId": "img-win", "OsName": "Windows Server 2022", "Platform": "Windows"}]
        elif action == "AllocateAddresses" and st.get("eip_quota") is not None and len(st["eips"]) >= st["eip_quota"]:
            r = {"Error": {"Code": "AddressQuotaLimitExceeded.DailyAllocate", "Message": "daily quota"}}
        elif action == "AllocateAddresses":
            assert p["InternetChargeType"] == "TRAFFIC_POSTPAID_BY_HOUR" and p["InternetMaxBandwidthOut"] >= 10
            eid = f"eip-{len(st['eips'])}"
            st["eips"][eid] = {"AddressId": eid, "AddressIp": next(eip_ips), "AddressStatus": "UNBIND",
                               "InstanceId": None}
            r["AddressSet"] = [eid]
        elif action == "DescribeAddresses":
            items = [e for k, e in st["eips"].items() if k not in st["released"]]
            if p.get("AddressIds"):
                items = [e for e in items if e["AddressId"] in p["AddressIds"]]
            for f in p.get("Filters", []):
                if f["Name"] == "instance-id":
                    items = [e for e in items if e["InstanceId"] in f["Values"]]
            r["AddressSet"] = items
        elif action == "ReleaseAddresses":
            eid = p["AddressIds"][0]
            if st["eips"][eid]["InstanceId"]:  # only UNBIND EIPs can be released
                r = {"Error": {"Code": "InvalidAddressState", "Message": "bound"}}
            else:
                st["released"].append(eid)
        elif action == "DescribeSecurityGroups":
            r["SecurityGroupSet"] = [{"SecurityGroupId": s} for s in st["sg"]]
        elif action == "CreateSecurityGroup":
            st["sg"].append("sg-1")
            r["SecurityGroup"] = {"SecurityGroupId": "sg-1"}
        elif action == "CreateSecurityGroupPolicies":
            if len(p["SecurityGroupPolicySet"]) != 1:
                r = {"Error": {"Code": "InvalidParameter", "Message": "one direction per call"}}
            else:
                st["rules"].append(p)
        elif action == "DescribeAddressQuota":
            r["QuotaSet"] = [{"QuotaId": "DAILY_EIP_APPLY", "QuotaCurrent": 3, "QuotaLimit": 40}]
        elif action == "DisassociateAddress":
            e = st["eips"][p["AddressId"]]
            e["InstanceId"], e["AddressStatus"] = None, "UNBIND"
        elif action == "RunInstances":
            st["run"].append(p)
            iid = f"ins-{len(st['instances'])}"
            st["instances"][iid] = {"InstanceId": iid, "InstanceName": p["InstanceName"], "InstanceState": "PENDING",
                                    "PublicIpAddresses": None if not p["InternetAccessible"]["PublicIpAssigned"]
                                    else ["1.1.1.1"], "Placement": {"Zone": p["Placement"]["Zone"]}}
            r["InstanceIdSet"] = [iid]
        elif action == "DescribeInstances":
            items = list(st["instances"].values())
            if p.get("InstanceIds"):
                items = [i for i in items if i["InstanceId"] in p["InstanceIds"]]
            for i in items:  # PENDING for two polls, then RUNNING
                st["polls"][i["InstanceId"]] = st["polls"].get(i["InstanceId"], 0) + 1
                if st["polls"][i["InstanceId"]] > 2:
                    i["InstanceState"] = "RUNNING"
            r["InstanceSet"] = [i for i in items if i["InstanceId"] not in st["terminated"]]
        elif action == "AssociateAddress":
            inst = st["instances"][p["InstanceId"]]
            if inst["PublicIpAddresses"]:
                r = {"Error": {"Code": "InvalidInstanceId.AlreadyHasPublicIp", "Message": "has a public IP"}}
            elif inst["InstanceState"] != "RUNNING":
                r = {"Error": {"Code": "InvalidInstanceState", "Message": "not running"}}
            else:
                st["assoc"].append((p["AddressId"], p["InstanceId"]))
                st["eips"][p["AddressId"]]["InstanceId"] = p["InstanceId"]
                inst["PublicIpAddresses"] = [st["eips"][p["AddressId"]]["AddressIp"]]
        elif action == "TerminateInstances":
            st["terminated"] += p["InstanceIds"]
        else:
            r = {"Error": {"Code": "InvalidAction", "Message": action}}
        return web.json_response({"Response": r})
    app.router.add_post("/{service}", handle)
    return app, st


async def scenario():
    assert "tencent" in PROVIDERS and "tencent" in RESERVE_PROVIDERS and "tencent" in SCOUT_PROVIDERS
    assert tencent.split_secret("id1\nkey2") == ("id1", "key2") and tencent.split_secret("id1:key2") == ("id1", "key2")
    for _ in range(50):
        pw = tencent.make_password()
        assert 8 <= len(pw) <= 30 and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
    app, st = fake_tencent(["43.133.1.1", "43.133.2.2", "43.156.5.7"])
    srv = TestServer(app)
    await srv.start_server()
    base = str(srv.make_url("")).rstrip("/")
    try:
        bad = tencent.TencentClient("AKIDtest123456 wrongkey", base_url=base)
        async with bad:
            try:
                await bad.check()
                raise AssertionError("bad key accepted")
            except tencent.ProviderError as e:
                assert e.status == 401 and "SecretKey" in str(e)
        assert type(make_client("tencent", "", SECRET, "")).__name__ == "TencentClient"
        async with tencent.TencentClient(SECRET, base_url=base) as c:
            info = await c.check()
            assert info.startswith("AKIDtest12") and "EIP сегодня: 3/40" in info, info
            regs = {r.region: r for r in await c.region_statuses()}
            assert regs["eu-frankfurt"].available and regs["eu-frankfurt"].country == "DE"
            assert regs["eu-frankfurt"].city == "Франкфурт" and not regs["eu-moscow"].available
            flavors = await c.list_flavors("eu-frankfurt")
            assert [f.id for f in flavors] == ["eu-frankfurt-2/S5.SMALL1", "eu-frankfurt-1/S5.MEDIUM2"], flavors
            assert flavors[0].ram_mb == 1024 and "$0.0200/час" in flavors[0].price
            assert [i.id for i in await c.list_images("eu-frankfurt", flavors[0])] == ["img-ubuntu"]

            job = PurchaseJob("eu-frankfurt", flavors[0], "img-ubuntu", None, ["43.156.5"], 1, max_attempts=0,
                              parallel=1, active_timeout=30, boot_volume_gb=10, port_parallel=3,
                              bot_key=("k", BOT))
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            assert [k.ipv4[0] for k in job.kept] == ["43.156.5.7"], job.log
            kept = job.kept[0]
            assert kept.admin_pass and len(st["run"]) == 1, "one server, built only for the right IP"
            run = st["run"][0]
            assert run["InternetAccessible"]["PublicIpAssigned"] is False and run["Placement"]["Zone"] == "eu-frankfurt-2"
            assert run["InternetAccessible"]["InternetMaxBandwidthOut"] == 0, "no own public IP, or the EIP can't bind"
            assert run["InstanceChargeType"] == "POSTPAID_BY_HOUR" and run["SecurityGroupIds"] == ["sg-1"]
            assert run["LoginSettings"]["Password"] == kept.admin_pass
            script = base64.b64decode(run["UserData"]).decode()
            assert BOT in script and "ufw disable" in script and "PasswordAuthentication yes" in script
            rule = {"Protocol": "ALL", "CidrBlock": "0.0.0.0/0", "Action": "ACCEPT"}
            assert [r["SecurityGroupPolicySet"] for r in st["rules"]] == [{"Ingress": [rule]}, {"Egress": [rule]}]
            good = next(k for k, e in st["eips"].items() if e["AddressIp"] == "43.156.5.7")
            assert st["assoc"] == [(good, kept.id)], "EIP bound once the server runs"
            assert set(st["released"]) == set(st["eips"]) - {good}, "wrong EIPs released"

            servers = await c.list_servers()
            assert [(s.id, s.ipv4, s.region) for s in servers] == [(kept.id, ["43.156.5.7"], "eu-frankfurt")] * 1 \
                or {s.id for s in servers} == {kept.id}
            await c.delete_server("eu-frankfurt", kept.id)
        await tencent.wait_pending(5)
        assert st["terminated"] == [kept.id] and good in st["released"], "server and its EIP are gone"

        # daily EIP quota used up → the purchase goes on with servers (own public IP), not a stop
        st["eip_quota"] = len(st["eips"])
        async with tencent.TencentClient(SECRET, base_url=base) as c:
            fl = (await c.list_flavors("eu-frankfurt"))[0]
            job = PurchaseJob("eu-frankfurt", fl, "img-ubuntu", None, ["1.1.1"], 1, max_attempts=0, parallel=1,
                              active_timeout=30, boot_volume_gb=10, port_parallel=3)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        assert not job.error and job.mode == "servers" and [k.ipv4[0] for k in job.kept] == ["1.1.1.1"], job.log
        assert any("Лимит резервных IP" in line for line in job.log)
    finally:
        await srv.close()


def test_tencent():
    orig = buyer.asyncio.sleep
    tencent.asyncio.sleep = buyer.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    try:
        asyncio.run(scenario())
    finally:
        tencent.asyncio.sleep = buyer.asyncio.sleep = orig


if __name__ == "__main__":
    test_tencent()
    print("OK")
