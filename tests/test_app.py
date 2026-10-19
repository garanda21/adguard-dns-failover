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
