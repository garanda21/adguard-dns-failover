from datetime import datetime

import pytest
import responses

from app import main as m
from app.config import ConfigError, load_config
from app.detector import evaluate
from app.state import BackupStore

API = "https://api.test/data.json"
AG = "http://adguard.test"


def payload(blocked_digi: int, total_digi: int = 4):
    data = []
    for i in range(total_digi):
        data.append({"isp": "DIGI", "stateChanges": [{"state": False}, {"state": i < blocked_digi}]})
    data.append({"isp": "Movistar", "stateChanges": [{"state": False}]})
    data.append({"isp": "Movistar", "stateChanges": []})
    return {"lastUpdate": "now", "data": data}


def test_evaluate_min_ips():
    # Real-world shape: thousands of IPs per ISP, a block hits only a few hundred.
    data = [{"isp": "DIGI", "stateChanges": [{"state": i < 300}]} for i in range(4000)]
    assert evaluate({"data": data}, 50, 20).active is True
    noise = [{"isp": "DIGI", "stateChanges": [{"state": i < 6}]} for i in range(4000)]
    assert evaluate({"data": noise}, 50, 20).active is False


def test_evaluate_threshold():
    assert evaluate(payload(2), 50).active is True
    assert evaluate(payload(1), 50).active is False
    st = evaluate(payload(3), 50)
    digi = next(s for s in st.isps if s.isp == "DIGI")
    assert (digi.blocked, digi.total) == (3, 4)


def env(tmp_path, **extra):
    base = {
        "ADGUARD_1_URL": AG + "/",
        "ADGUARD_1_USER": "u",
        "ADGUARD_1_PASSWORD": "p",
        "API_URL": API,
        "DATA_DIR": str(tmp_path),
        "CRON_SCHEDULES": "0 */4 * * 1-5; 0 */2 * * 6,0",
    }
    base.update(extra)
    return base


def test_config_parsing(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("s3cret\n")
    cfg = load_config(env(tmp_path, ADGUARD_10_URL="https://b.test", ADGUARD_10_PASSWORD_FILE=str(secret),
                          ADGUARD_2_URL="http://c.test"))
    assert [i.id for i in cfg.instances] == ["1", "2", "10"]
    assert cfg.instances[0].url == AG
    assert cfg.instances[2].password == "s3cret"
    assert cfg.cron_schedules == ["0 */4 * * 1-5", "0 */2 * * 6,0"]
    assert cfg.block_upstreams[0] == "https://dns.xdp.es/dns-query"


def test_config_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_config({"DATA_DIR": str(tmp_path)})
    with pytest.raises(ConfigError):
        load_config(env(tmp_path, CRON_SCHEDULES="bad cron"))


def test_next_run_multiple_crons():
    # Friday 2026-10-16 21:10 -> weekday cron (every 4h) next at Sat 00:00 from weekend cron
    now = datetime(2026, 10, 16, 21, 10)
    assert m.next_run(["0 */4 * * 1-5", "0 */2 * * 6,0"], now) == datetime(2026, 10, 17, 0, 0)
    # Saturday 09:30 -> 10:00 (weekend every 2h)
    assert m.next_run(["0 */4 * * 1-5", "0 */2 * * 6,0"], datetime(2026, 10, 17, 9, 30)) == datetime(2026, 10, 17, 10, 0)


@responses.activate
def test_block_cycle(tmp_path):
    cfg = load_config(env(tmp_path))
    store = BackupStore(cfg.data_dir)
    original = {"upstream_dns": ["1.1.1.1"], "bootstrap_dns": ["9.9.9.9"], "fallback_dns": [], "upstream_mode": "parallel"}
    current = dict(original)

    def dns_info(_req):
        import json
        return 200, {}, json.dumps(current)

    def dns_config(req):
        import json
        current.update(json.loads(req.body))
        return 200, {}, "OK"

    responses.add_callback(responses.GET, f"{AG}/control/dns_info", callback=dns_info)
    responses.add_callback(responses.POST, f"{AG}/control/dns_config", callback=dns_config)
    responses.add(responses.POST, f"{AG}/control/cache_clear", body="OK")

    # Block starts
    responses.add(responses.GET, API, json=payload(4))
    assert m.run_once(cfg, store) == 0
    assert current["upstream_dns"] == cfg.block_upstreams
    assert current["bootstrap_dns"] == cfg.block_bootstrap
    assert current["fallback_dns"] == cfg.block_upstreams
    assert store.get("1")["dns"]["upstream_dns"] == ["1.1.1.1"]

    # Still blocked: backup must not be overwritten with block DNS values
    assert m.run_once(cfg, store) == 0
    assert store.get("1")["dns"]["upstream_dns"] == ["1.1.1.1"]

    # Block ends
    responses.replace(responses.GET, API, json=payload(0))
    assert m.run_once(cfg, store) == 0
    assert current == original
    assert store.get("1") is None
    clears = [c for c in responses.calls if c.request.url.endswith("cache_clear")]
    assert len(clears) == 2
    assert responses.calls[1].request.headers["Authorization"].startswith("Basic ")


@responses.activate
def test_api_failure_leaves_dns_untouched(tmp_path):
    cfg = load_config(env(tmp_path))
    responses.add(responses.GET, API, status=500)
    assert m.run_once(cfg, BackupStore(cfg.data_dir)) == 1
    assert len(responses.calls) == 1


@responses.activate
def test_telegram_notifies_only_on_transitions(tmp_path):
    from app.notifier import TelegramNotifier

    cfg = load_config(env(tmp_path, ADGUARD_1_NAME="Casa", TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="111;222"))
    store = BackupStore(cfg.data_dir)
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_ids)
    current = {"upstream_dns": ["1.1.1.1"], "bootstrap_dns": ["9.9.9.9"]}

    def dns_config(req):
        import json
        current.update(json.loads(req.body))
        return 200, {}, "OK"

    responses.add_callback(responses.GET, f"{AG}/control/dns_info",
                           callback=lambda r: (200, {}, __import__("json").dumps(current)))
    responses.add_callback(responses.POST, f"{AG}/control/dns_config", callback=dns_config)
    responses.add(responses.POST, f"{AG}/control/cache_clear", body="OK")
    tg = responses.add(responses.POST, "https://api.telegram.org/botT/sendMessage", json={"ok": True})

    responses.add(responses.GET, API, json=payload(4))
    m.run_once(cfg, store, notifier)
    m.run_once(cfg, store, notifier)  # no transition, no message
    assert tg.call_count == 2  # one per chat id
    import json
    body = json.loads(tg.calls[0].request.body)
    assert "Bloqueo de IPs detectado" in body["text"] and "Casa" in body["text"]

    responses.replace(responses.GET, API, json=payload(0))
    m.run_once(cfg, store, notifier)
    assert tg.call_count == 4
    assert "Fin del bloqueo" in json.loads(tg.calls[-1].request.body)["text"]


def test_fallback_opt_out(tmp_path):
    assert load_config(env(tmp_path, BLOCK_FALLBACK="none")).block_fallback == []
    assert load_config(env(tmp_path, BLOCK_FALLBACK="8.8.8.8")).block_fallback == ["8.8.8.8"]
    assert "fallback_dns" not in m.block_payload(load_config(env(tmp_path, BLOCK_FALLBACK="none")))


def test_is_block_dns_active_detects_manual_setup(tmp_path):
    cfg = load_config(env(tmp_path))
    assert m.is_block_dns_active(cfg, {"upstream_dns": ["https://dns.xdp.es/dns-query"]})
    assert m.is_block_dns_active(cfg, {"upstream_dns": ["tls://dns.xdp.es", "85.208.114.51"]})
    assert not m.is_block_dns_active(cfg, {"upstream_dns": ["https://dns.xdp.es/dns-query", "1.1.1.1"]})
    assert not m.is_block_dns_active(cfg, {"upstream_dns": ["https://dns10.quad9.net/dns-query"]})
    assert not m.is_block_dns_active(cfg, {"upstream_dns": []})


def test_isps_filter():
    data = [{"isp": "DIGI", "stateChanges": [{"state": True}]}] * 30
    data += [{"isp": "Movistar", "stateChanges": [{"state": False}]}] * 30
    payload = {"data": data}
    assert evaluate(payload, 50, 20).active is True
    st = evaluate(payload, 50, 20, ["movistar"])
    assert st.active is False and [s.isp for s in st.isps] == ["Movistar"]
    assert evaluate(payload, 50, 20, ["Movistar", " digi "]).active is True
    assert evaluate(payload, 50, 20, ["movistar", "jazztel"]).unknown_isps == ["jazztel"]


def test_isps_config(tmp_path):
    assert load_config(env(tmp_path)).isps == []
    assert load_config(env(tmp_path, ISPS="movistar, Orange;digi")).isps == ["movistar", "Orange", "digi"]


PH = "http://pihole.test"


def pihole_mock(current, read_only=False):
    import json

    def get_upstreams(req):
        if "detailed=true" in req.url:
            return 200, {}, json.dumps({"config": {"dns": {"upstreams": {
                "value": current["upstreams"], "flags": {"env_var": read_only}}}}})
        return 200, {}, json.dumps({"config": {"dns": {"upstreams": current["upstreams"]}}})

    def patch(req):
        assert req.headers["X-FTL-SID"] == "SID1"
        current["upstreams"] = json.loads(req.body)["config"]["dns"]["upstreams"]
        return 200, {}, "{}"

    responses.add(responses.POST, f"{PH}/api/auth",
                  json={"session": {"valid": True, "sid": "SID1", "validity": 1800}})
    responses.add(responses.DELETE, f"{PH}/api/auth", status=204)
    responses.add_callback(responses.GET, f"{PH}/api/config/dns/upstreams", callback=get_upstreams)
    responses.add_callback(responses.PATCH, f"{PH}/api/config", callback=patch)
    responses.add(responses.POST, f"{PH}/api/action/restartdns", json={"status": "ok"})


def pihole_env(tmp_path, **extra):
    base = {"PIHOLE_1_URL": PH, "PIHOLE_1_PASSWORD": "app-pass", "API_URL": API, "DATA_DIR": str(tmp_path)}
    base.update(extra)
    return base


@responses.activate
def test_pihole_block_cycle(tmp_path):
    cfg = load_config(pihole_env(tmp_path))
    store = BackupStore(cfg.data_dir)
    inst = cfg.instances[0]
    assert (inst.kind, inst.key, inst.name) == ("pihole", "pihole_1", f"Pi-hole #1 ({PH})")
    current = {"upstreams": ["8.8.8.8", "1.1.1.1#53"]}
    pihole_mock(current)

    responses.add(responses.GET, API, json=payload(4))
    assert m.run_once(cfg, store) == 0
    assert current["upstreams"] == ["85.208.114.51", "51.170.52.10"]
    assert store.get("pihole_1")["dns"] == {"upstreams": ["8.8.8.8", "1.1.1.1#53"]}
    assert m.run_once(cfg, store) == 0  # still blocked: no change, backup kept
    assert store.get("pihole_1")["dns"]["upstreams"] == ["8.8.8.8", "1.1.1.1#53"]

    responses.replace(responses.GET, API, json=payload(0))
    assert m.run_once(cfg, store) == 0
    assert current["upstreams"] == ["8.8.8.8", "1.1.1.1#53"]
    assert store.get("pihole_1") is None

    calls = [c.request for c in responses.calls]
    assert sum(1 for r in calls if r.url.endswith("/api/action/restartdns")) == 2
    # Every login is paired with a logout so API seats are released.
    assert sum(1 for r in calls if r.method == "POST" and r.url.endswith("/api/auth")) == \
        sum(1 for r in calls if r.method == "DELETE")


@responses.activate
def test_pihole_read_only_upstreams(tmp_path):
    cfg = load_config(pihole_env(tmp_path))
    pihole_mock({"upstreams": ["8.8.8.8"]}, read_only=True)
    responses.add(responses.GET, API, json=payload(4))
    assert m.run_once(cfg, BackupStore(cfg.data_dir)) == 1
    assert not any(c.request.method == "PATCH" for c in responses.calls)
    assert BackupStore(cfg.data_dir).get("pihole_1") is None  # no unrestorable backup


@responses.activate
def test_pihole_app_sudo_forbidden_discards_backup(tmp_path):
    cfg = load_config(pihole_env(tmp_path))
    store = BackupStore(cfg.data_dir)
    pihole_mock({"upstreams": ["8.8.8.8"]})
    responses.replace(responses.PATCH, f"{PH}/api/config", status=403, json={"error": {
        "key": "forbidden", "hint": "(webserver.api.app_sudo is false)"}})
    responses.add(responses.GET, API, json=payload(4))
    assert m.run_once(cfg, store) == 1
    assert store.get("pihole_1") is None


@responses.activate
def test_pihole_bad_password(tmp_path):
    cfg = load_config(pihole_env(tmp_path))
    responses.add(responses.POST, f"{PH}/api/auth", status=401, json={"session": {"valid": False}})
    responses.add(responses.GET, API, json=payload(4))
    assert m.run_once(cfg, BackupStore(cfg.data_dir)) == 1


def test_mixed_instances_config(tmp_path):
    cfg = load_config(env(tmp_path, PIHOLE_1_URL=PH, BLOCK_PIHOLE_UPSTREAMS="9.9.9.9#53"))
    assert [(i.kind, i.key) for i in cfg.instances] == [("adguard", "1"), ("pihole", "pihole_1")]
    assert cfg.block_pihole_upstreams == ["9.9.9.9#53"]


def test_message_when_every_instance_failed():
    from app.detector import BlockStatus, IspStatus
    from app.notifier import build_message

    st = BlockStatus(True, [IspStatus("DIGI", 10, 5)], "x")
    msg = build_message(True, st, [], ["NAS"], ["1.2.3.4"])
    assert "No se ha podido cambiar" in msg and "restablecida" not in msg and "AdGuard" not in msg
    ok = build_message(True, st, ["NAS"], [], ["1.2.3.4"])
    assert "Tus servidores DNS han cambiado" in ok and "restablecida" in ok
    assert "No se han podido restaurar" in build_message(False, st, [], ["NAS"], [])


def test_verify_ssl_option(tmp_path):
    from app.pihole import PiHoleClient

    cfg = load_config(pihole_env(tmp_path, PIHOLE_1_URL="https://ph.test:8443", PIHOLE_1_VERIFY_SSL="false"))
    assert cfg.instances[0].verify_ssl is False
    assert PiHoleClient(cfg.instances[0]).http.verify is False
    assert load_config(env(tmp_path)).instances[0].verify_ssl is True


def app_password_login(app_sudo):
    responses.replace(responses.POST, f"{PH}/api/auth", json={"session": {
        "valid": True, "sid": "SID1", "validity": 1800, "message": "app-password correct"}})
    responses.add(responses.GET, f"{PH}/api/config/webserver/api/app_sudo",
                  json={"config": {"webserver": {"api": {"app_sudo": app_sudo}}}})


@responses.activate
def test_pihole_app_password_without_sudo_is_caught_before_backup(tmp_path):
    cfg = load_config(pihole_env(tmp_path))
    store = BackupStore(cfg.data_dir)
    pihole_mock({"upstreams": ["8.8.8.8"]})
    app_password_login(False)
    responses.add(responses.GET, API, json=payload(4))
    assert m.run_once(cfg, store) == 1
    assert store.get("pihole_1") is None
    assert not any(c.request.method == "PATCH" for c in responses.calls)


@responses.activate
def test_check_instances_ok_and_failures(tmp_path):
    from app.notifier import TelegramNotifier

    cfg = load_config(pihole_env(tmp_path, TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="1"))
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_ids)
    tg = responses.add(responses.POST, "https://api.telegram.org/botT/sendMessage", json={"ok": True})
    current = {"upstreams": ["8.8.8.8"]}
    pihole_mock(current)
    app_password_login(True)
    assert m.check_instances(cfg, notifier) == []
    assert tg.call_count == 1  # startup "all good" summary
    import json
    assert "operativo" in json.loads(tg.calls[0].request.body)["text"]
    assert current["upstreams"] == ["8.8.8.8"]  # read-only check, nothing changed

    responses.replace(responses.GET, f"{PH}/api/config/webserver/api/app_sudo",
                      json={"config": {"webserver": {"api": {"app_sudo": False}}}})
    assert m.check_instances(cfg, notifier) == [PH.removeprefix("http://")]
    import json
    text = json.loads(tg.calls[1].request.body)["text"]
    assert "Problema de conexión" in text and "app_sudo" in text


@responses.activate
def test_startup_message_can_be_disabled(tmp_path):
    from app.notifier import TelegramNotifier

    cfg = load_config(pihole_env(tmp_path, TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="1",
                                 TELEGRAM_STARTUP_MESSAGE="false"))
    tg = responses.add(responses.POST, "https://api.telegram.org/botT/sendMessage", json={"ok": True})
    pihole_mock({"upstreams": ["8.8.8.8"]})
    assert m.check_instances(cfg, TelegramNotifier("T", ["1"])) == []
    assert tg.call_count == 0


@responses.activate
def test_pihole_restart_failure_after_write_keeps_backup(tmp_path):
    cfg = load_config(pihole_env(tmp_path))
    store = BackupStore(cfg.data_dir)
    current = {"upstreams": ["8.8.8.8"]}
    pihole_mock(current)
    responses.replace(responses.POST, f"{PH}/api/action/restartdns", status=500)
    responses.add(responses.GET, API, json=payload(4))
    # Config was written, so the switch counts as done and the backup must survive.
    assert m.run_once(cfg, store) == 0
    assert current["upstreams"] == cfg.block_pihole_upstreams
    assert store.get("pihole_1")["dns"]["upstreams"] == ["8.8.8.8"]

    responses.replace(responses.GET, API, json=payload(0))
    assert m.run_once(cfg, store) == 0
    assert current["upstreams"] == ["8.8.8.8"]


@responses.activate
def test_adguard_cache_clear_failure_after_write_keeps_backup(tmp_path):
    import json

    cfg = load_config(env(tmp_path))
    store = BackupStore(cfg.data_dir)
    current = {"upstream_dns": ["1.1.1.1"], "bootstrap_dns": ["9.9.9.9"]}

    def dns_config(req):
        current.update(json.loads(req.body))
        return 200, {}, "OK"

    responses.add_callback(responses.GET, f"{AG}/control/dns_info", callback=lambda r: (200, {}, json.dumps(current)))
    responses.add_callback(responses.POST, f"{AG}/control/dns_config", callback=dns_config)
    responses.add(responses.POST, f"{AG}/control/cache_clear", status=500)
    responses.add(responses.GET, API, json=payload(4))
    assert m.run_once(cfg, store) == 0
    assert store.get("1")["dns"]["upstream_dns"] == ["1.1.1.1"]


@responses.activate
def test_mixed_notification_shows_upstreams_of_changed_kinds(tmp_path):
    import json

    from app.notifier import TelegramNotifier

    cfg = load_config(env(tmp_path, PIHOLE_1_URL=PH, PIHOLE_1_PASSWORD="app-pass",
                          TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="1"))
    store = BackupStore(cfg.data_dir)
    # AdGuard is unreachable; only Pi-hole changes.
    responses.add(responses.GET, f"{AG}/control/dns_info", status=500)
    pihole_mock({"upstreams": ["8.8.8.8"]})
    tg = responses.add(responses.POST, "https://api.telegram.org/botT/sendMessage", json={"ok": True})
    responses.add(responses.GET, API, json=payload(4))
    m.run_once(cfg, store, TelegramNotifier("T", ["1"]))
    text = json.loads(tg.calls[0].request.body)["text"]
    assert "85.208.114.51" in text and "dns-query" not in text

    assert m.shown_upstreams(cfg, {"adguard", "pihole"}) == [
        "https://dns.xdp.es/dns-query", "tls://dns.xdp.es", "85.208.114.51", "51.170.52.10"]


def test_describe_cron_in_spanish():
    from app.schedule import describe_cron

    assert describe_cron("0 */4 * * 1-5") == "cada 4 horas, de lunes a viernes"
    assert describe_cron("0 */2 * * 6,0") == "cada 2 horas, sólo el sábado y domingo"
    assert describe_cron("*/30 * * * *") == "cada 30 minutos"
    assert describe_cron("not a cron") == "not a cron"


def test_startup_message_includes_schedule():
    from app.notifier import build_check_message

    msg = build_check_message(["NAS"], [], False, ["0 */4 * * 1-5", "0 */2 * * 6,0"])
    assert "Comprobaré los bloqueos" in msg and "de lunes a viernes" in msg and "Próxima comprobación" in msg
    assert "Comprobaré" not in build_check_message([], [("NAS", "err")], False, ["*/30 * * * *"])
