"""Click the location button in a real browser and require a visible outcome."""

import asyncio
import json
import os
import socket
import sys
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

websockets = pytest.importorskip("websockets")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    if not shutil_which("google-chrome"):
        pytest.skip("google-chrome no está disponible")
    port = _free_port()
    debug = _free_port()
    token = "prueba-navegador-local-v14-segura"
    env = os.environ.copy()
    env.update({
        "AERDOPPLER_ACCESS_TOKEN": token,
        "AERDOPPLER_COOKIE_SECURE": "0",
        "PORT": str(port),
        "PYTHONPATH": "/workspace/aerodoppler/src",
    })
    server = subprocess.Popen(
        [sys.executable, "-m", "aerodoppler"],
        cwd="/workspace", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    profile = tmp_path_factory.mktemp("chrome")
    chrome = subprocess.Popen(
        ["google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox", f"--user-data-dir={profile}",
         f"--remote-debugging-port={debug}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait(f"http://127.0.0.1:{port}/healthz")
        version = json.load(urllib.request.urlopen(f"http://127.0.0.1:{debug}/json/version"))
        page = Browser(version["webSocketDebuggerUrl"], f"http://127.0.0.1:{port}/", token)
        page.open()
        yield page
        page.close()
    finally:
        chrome.terminate()
        server.terminate()
        chrome.wait(timeout=5)
        server.wait(timeout=5)


def test_province_menu_lists_melilla_and_a_warning(ui):
    ui.reload()
    deadline = time.time() + 8
    while time.time() < deadline:
        if ui.eval("!!document.querySelector('#locate-region option[value=melilla]')"):
            break
        time.sleep(0.2)
    assert ui.eval("!!document.querySelector('#locate-region option[value=melilla]')") is True, ui.eval("JSON.stringify(window.__errors)")
    assert ui.eval("!!document.querySelector('#locate-region option[value=badajoz]')") is True
    assert "Si el audio se grabó fuera" in ui.eval("document.body.innerText")
    ui.eval("document.querySelector('#locate-region').value = 'melilla'; document.querySelector('#locate-region').dispatchEvent(new Event('change'))")
    deadline = time.time() + 8
    text = ""
    while time.time() < deadline:
        text = ui.eval("document.querySelector('#locate-fit').textContent") or ""
        if "Cabe en este servicio" in text:
            break
        time.sleep(0.2)
    assert "Cabe en este servicio" in text
    assert "Melilla" in text


def test_locate_form_does_not_ask_for_a_place(ui):
    ui.reload()
    assert ui.eval("document.querySelector('#locate-lat')") is None
    assert ui.eval("document.querySelector('#locate-region').value") == "unsure"
    assert "opcional" in ui.eval("document.querySelector('label[for=locate-region]').textContent")
    assert "Averiguar dónde se grabó" in ui.text("h1")
    const_bytes = """
      const text = "2026-11-07T23:24";
      const payload = 1 + text.length;
      const bytes = new Uint8Array(10 + 10 + payload);
      bytes.set([0x49,0x44,0x33,3,0,0,0,0,0,0]);
      bytes.set([0x54,0x44,0x52,0x43, 0,0,0,payload, 0,0, 0], 10);
      for (let i = 0; i < text.length; i++) bytes[21 + i] = text.charCodeAt(i);
      clipClockFromBuffer(bytes);
    """
    found = ui.eval(const_bytes)
    assert found["localValue"] == "2026-11-07T23:24"
    assert "ID3" in found["note"]
    assert "modificación" not in found["note"]


def test_empty_click_shows_what_is_missing(ui):
    ui.reload()
    ui.click("#locate-submit")
    text = ui.text("#locate-out")
    assert "Elige un archivo de audio" in text
    assert "fecha y hora" in text
    assert ui.eval("document.querySelector('#locate-submit').disabled") is False


def test_click_shows_progress_then_server_error(ui, tmp_path):
    ui.reload()
    wav = tmp_path / "tiny.wav"
    wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    ui.set_value("#locate-when", "2026-11-07T23:24")
    ui.set_file("#locate-audio", wav)
    ui.eval("""
      const orig = window.fetch;
      window.fetch = async (...args) => {
        window.__progress = document.querySelector('#locate-out').textContent;
        window.__busy = document.querySelector('#locate-submit').disabled;
        return orig(...args);
      };
    """)
    ui.click("#locate-submit")
    ui.wait_text("No se pudo leer")
    assert "Analizando el audio" in ui.eval("window.__progress")
    assert ui.eval("window.__busy") is True
    assert ui.eval("document.querySelector('#locate-submit').disabled") is False


def test_network_failure_is_visible(ui, tmp_path):
    ui.reload()
    wav = tmp_path / "tiny.wav"
    wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    ui.set_value("#locate-when", "2026-11-07T23:24")
    ui.set_file("#locate-audio", wav)
    ui.eval("window.fetch = () => Promise.reject(new TypeError('Failed to fetch'))")
    ui.click("#locate-submit")
    ui.wait_text("No hay conexión")
    assert ui.eval("document.querySelector('#locate-submit').disabled") is False


def test_verdict_and_regions_render_in_spanish(ui):
    ui.reload()
    ui.eval("""
      renderLocation({
        verdict: "ambiguous",
        verdict_label: "Ambiguo",
        summary: "Varias zonas explican los datos.",
        show_point: false,
        point_is_certain: false,
        reported_point: null,
        hypotheses: [{lat: 40.02, lon: -3.98, rmse_hz: 1.2}],
        regions: {"68": {radius_m: 800, cells: []}, "90": {radius_m: 1600, cells: [{lat: 40.02, lon: -3.98}]}},
        clock: {shared: true, offset_s: 18, step_s: 2, spread_68_s: 2},
        flags: [{message: "Hay otro mínimo."}],
        resolution_m: 375
      });
    """)
    text = ui.text("#locate-out")
    assert "Veredicto: Ambiguo" in text
    assert "No se marca un punto ganador" in text
    assert "Región del 68 %: 800 m" in text
    assert "Región del 90 %: 1600 m" in text
    assert "Desfase de reloj común" in text
    assert "18.0 s" in text
    assert "Ningún punto se muestra como seguro" in text
    ui.eval("""
      renderLocation({
        verdict: "failed",
        verdict_label: "Insuficiente",
        summary: "Los datos no sostienen una posición.",
        show_point: false,
        hypotheses: [],
        regions: {"68": {radius_m: null, cells: []}, "90": {radius_m: null, cells: []}},
        clock: {shared: false}
      });
    """)
    failed = ui.text("#locate-out")
    assert "Veredicto: Insuficiente" in failed
    assert "No hay región del 68 %" in failed
    assert "no se separa el lugar del desfase" in failed


def test_timeout_is_visible(ui, tmp_path):
    ui.reload()
    wav = tmp_path / "tiny.wav"
    wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    ui.set_value("#locate-when", "2026-11-07T23:24")
    ui.set_file("#locate-audio", wav)
    ui.eval("""
      window.fetch = () => new Promise((_resolve, reject) => {
        setTimeout(() => reject(new DOMException('aborted', 'AbortError')), 300);
      });
    """)
    ui.click("#locate-submit")
    ui.wait_text("tiempo de espera", timeout=5)
    assert ui.eval("document.querySelector('#locate-submit').disabled") is False


class Browser:
    def __init__(self, websocket_url, page_url, token):
        self.websocket_url = websocket_url
        self.page_url = page_url
        self.token = token
        self.session = None
        self._next = 0
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def open(self):
        self._run(self._open())

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    def reload(self):
        self._run(self._reload())

    async def _reload(self):
        await self._call("Page.navigate", {"url": self.page_url})
        await self._loaded()

    def click(self, selector):
        self.eval(f"document.querySelector({json.dumps(selector)}).click()")

    def text(self, selector):
        return self.eval(f"document.querySelector({json.dumps(selector)}).innerText")

    def set_value(self, selector, value):
        self.eval(f"document.querySelector({json.dumps(selector)}).value = {json.dumps(value)}")

    def set_file(self, selector, path: Path):
        self._run(self._set_file(selector, path))

    def eval(self, expression):
        return self._run(self._eval(expression))

    def wait_text(self, snippet, timeout=8):
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = self.text("#locate-out") or ""
            if snippet in last:
                return last
            time.sleep(0.15)
        raise AssertionError(f"no apareció {snippet!r}; texto={last!r}")

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=20)

    async def _open(self):
        self._ws = await websockets.connect(self.websocket_url, max_size=8_000_000)
        target = await self._call("Target.createTarget", {"url": "about:blank"})
        attached = await self._call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        self.session = attached["sessionId"]
        await self._call("Page.enable")
        await self._call("Runtime.enable")
        await self._call("DOM.enable")
        await self._call("Network.enable")
        await self._call("Emulation.setDeviceMetricsOverride", {
            "width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True,
        })
        import hashlib
        import hmac
        cookie = hmac.new(self.token.encode(), b"aerodoppler-v14-session", hashlib.sha256).hexdigest()
        await self._call("Network.setCookie", {"name": "ad_session", "value": cookie, "url": self.page_url})
        await self._call("Page.navigate", {"url": self.page_url})
        await self._loaded()

    async def _eval(self, expression):
        result = await self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        details = result.get("exceptionDetails")
        if details:
            raise AssertionError(details.get("text") or details)
        return result.get("result", {}).get("value")

    async def _set_file(self, selector, path):
        document = await self._call("DOM.getDocument")
        found = await self._call("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": selector})
        await self._call("DOM.setFileInputFiles", {"nodeId": found["nodeId"], "files": [str(path)]})

    async def _loaded(self):
        for _ in range(40):
            result = await self._call("Runtime.evaluate", {
                "expression": "document.querySelector('#locate-submit') && window.__appReady ? 'ready' : ''",
                "returnByValue": True,
            })
            if result.get("result", {}).get("value") == "ready":
                return
            await asyncio.sleep(0.1)
        raise AssertionError("la página no mostró el botón")

    async def _call(self, method, params=None):
        self._next += 1
        message = {"id": self._next, "method": method, "params": params or {}}
        if self.session:
            message["sessionId"] = self.session
        await self._ws.send(json.dumps(message))
        while True:
            raw = json.loads(await self._ws.recv())
            if raw.get("id") == self._next:
                if "error" in raw:
                    raise AssertionError(raw["error"])
                return raw.get("result") or {}


def _wait(url):
    for _ in range(40):
        try:
            urllib.request.urlopen(url, timeout=0.3)
            return
        except Exception:
            time.sleep(0.15)
    raise AssertionError(url)


def shutil_which(name):
    from shutil import which
    return which(name)

