# qmhs_challenges_inc.py  • PART 1
from __future__ import annotations
import asyncio, json, logging, os, random, secrets, threading, time, hashlib, textwrap, math
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Tuple, Optional
from base64 import b64encode, b64decode

import cv2, psutil, aiosqlite, httpx, numpy as np, pennylane as qml, tkinter as tk
import tkinter.simpledialog as sd
import tkinter.messagebox as mb
import bleach
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

MASTER_KEY    = os.path.expanduser("~/.cache/ci_qmhs_master_key.bin")
SETTINGS_FILE = "settings.enc.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
LOGGER = logging.getLogger("qmhs")

# === AES-GCM Crypto ===
class AESGCMCrypto:
    def __init__(self, path: str) -> None:
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            key = AESGCM.generate_key(bit_length=128)
            with open(self.path + ".tmp", "wb") as f:
                f.write(key)
            os.replace(self.path + ".tmp", self.path)
            os.chmod(self.path, 0o600)
        with open(self.path, "rb") as f:
            self.key = f.read()
        self.aes = AESGCM(self.key)

    def encrypt(self, data: bytes | str) -> bytes:
        if isinstance(data, str): data = data.encode()
        nonce = secrets.token_bytes(12)
        return b64encode(nonce + self.aes.encrypt(nonce, data, None))

    def decrypt(self, blob: bytes | str) -> bytes:
        raw = b64decode(blob)
        return self.aes.decrypt(raw[:12], raw[12:], None)

# === Settings ===
@dataclass
class Settings:
    location: str = "Mobile-Unit-Upstate"
    staff_ratio: str = "2 Peer Nav:1 Participant"
    emerg_contact: str = "864-214-6181"
    seclusion_room: bool = False
    psychiatrist_eta: str = "15 min"
    mode_autonomous: bool = True

    cpu_cores: int = psutil.cpu_count(logical=False) or 2
    total_ram_gb: float = round(psutil.virtual_memory().total / 1e9, 1)
    gpu_available: bool = False
    camera_idx: int = -1

    sampling_interval: float = 1.0
    cpu_threshold: float = 0.70
    mem_threshold: float = 0.75
    confidence_threshold: float = 0.75
    action_counts: Dict[str, int] = field(default_factory=lambda: {"Green": 1, "Amber": 3, "Red": 4})

    db_path: str = "ci_harmred_reports.db"
    api_key: str = ""
    qadapt_refresh_h: int = 12
    cev_window: int = 60
    hbe_enabled: bool = False
    fusion_dim: int = 64
    hipaa_lite: bool = True

    @classmethod
    def default(cls) -> Settings:
        load_dotenv()
        return cls(api_key=os.getenv("OPENAI_API_KEY", ""))

    @classmethod
    def load(cls, crypto: AESGCMCrypto) -> Settings:
        if not os.path.exists(SETTINGS_FILE):
            return cls.default()
        try:
            blob = open(SETTINGS_FILE, "rb").read()
            return cls(**json.loads(crypto.decrypt(blob).decode()))
        except Exception as e:
            LOGGER.error("Settings error, loading defaults: %s", e)
            return cls.default()

    def save(self, crypto: AESGCMCrypto) -> None:
        with open(SETTINGS_FILE, "wb") as f:
            f.write(crypto.encrypt(json.dumps(asdict(self)).encode()))

    def prompt_gui(self) -> None:
        mb.showinfo("Challenges QMHS Settings", "Enter or leave blank to keep current values.")
        ask = lambda p, d: bleach.clean(sd.askstring("Settings", p, initialvalue=str(d)) or str(d), strip=True)
        self.location         = ask("Location:", self.location)
        self.staff_ratio      = ask("Staff Ratio:", self.staff_ratio)
        self.emerg_contact    = ask("Emergency Hotline:", self.emerg_contact)
        self.seclusion_room   = ask("Is there a seclusion room? (y/n):", "n").startswith("y")
        self.psychiatrist_eta = ask("Clinician ETA:", self.psychiatrist_eta)
        self.mode_autonomous  = ask("Mode (autonomous/manual):", "autonomous").startswith("a")
        self.cpu_cores        = int(ask("CPU Cores:", self.cpu_cores))
        self.total_ram_gb     = float(ask("Total RAM (GB):", self.total_ram_gb))
        self.gpu_available    = ask("GPU available? (y/n):", "n").startswith("y")
        self.camera_idx       = int(ask("Camera Index:", self.camera_idx))
        self.api_key          = ask("OpenAI API Key:", self.api_key)
        self.hipaa_lite       = ask("Enable HIPAA-lite export? (y/n):", "y").startswith("y")

# === Report DB ===
class ReportDB:
    def __init__(self, path: str, crypto: AESGCMCrypto) -> None:
        self.path = path
        self.crypto = crypto
        self.conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, ts REAL, blob BLOB)"
        )
        await self.conn.commit()

    async def save(self, ts: float, payload: Dict[str, Any]) -> None:
        blob = self.crypto.encrypt(json.dumps(payload).encode())
        await self.conn.execute("INSERT INTO scans(ts, blob) VALUES (?, ?)", (ts, blob))
        await self.conn.commit()

    async def list_reports(self) -> List[Tuple[int, float]]:
        cur = await self.conn.execute("SELECT id, ts FROM scans ORDER BY ts DESC")
        return await cur.fetchall()

    async def load(self, row_id: int) -> Dict[str, Any]:
        cur = await self.conn.execute("SELECT blob FROM scans WHERE id = ?", (row_id,))
        res = await cur.fetchone()
        if not res:
            raise ValueError("No report with that ID.")
        return json.loads(bleach.clean(self.crypto.decrypt(res[0]).decode(), strip=True))

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

# === OpenAI Client ===
@dataclass
class OpenAIClient:
    api_key: str
    model: str = "gpt-4o"
    url: str = "https://api.openai.com/v1/chat/completions"
    timeout: float = 25.0
    retries: int = 4

    async def chat(self, prompt: str, max_tokens: int) -> str:
        if not self.api_key:
            raise RuntimeError("Missing OpenAI API key.")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.25,
            "max_tokens": max_tokens
        }
        delay = 1.0
        for attempt in range(1, self.retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as cli:
                    r = await cli.post(self.url, headers=headers, json=body)
                    r.raise_for_status()
                    return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                if attempt == self.retries:
                    raise
                wait = delay + random.uniform(0, 0.5)
                LOGGER.warning("Retry %d/%d on OpenAI call (%.1fs delay)", attempt, self.retries, wait)
                await asyncio.sleep(wait)
                delay *= 2

# === BioVector ===
@dataclass
class BioVector:
    arr: np.ndarray = field(repr=False)

    @staticmethod
    def from_frame(frame: np.ndarray) -> "BioVector":
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0], None, [9], [0, 180]).flatten()
        hist /= hist.sum() + 1e-6
        vec = np.concatenate([
            hist,
            [hsv[..., 1].mean() / 255.0, frame.mean() / 255.0],
            np.zeros(25 - 11),
        ])
        return BioVector(vec.astype(np.float32))

# === Advanced 7-Qubit Quantum Logic ===
DEV = qml.device("default.qubit", wires=7)

def _layer_rotations(params: List[float]) -> None:
    for w, (rx, ry, rz) in enumerate(zip(*[iter(params)] * 3)):
        qml.RX(rx, wires=w)
        qml.RY(ry, wires=w)
        qml.RZ(rz, wires=w)

def _layer_entangle() -> None:
    for w in range(7):
        qml.CZ(wires=[w, (w + 1) % 7])

@qml.qnode(DEV)
def q_intensity7(theta: float, env: Tuple[float, float], colour_seed: Optional[List[float]] = None) -> float:
    if colour_seed is None:
        colour_seed = [0.25 * math.pi] * 7
    for w, phi in enumerate(colour_seed):
        qml.RY(phi, wires=w)
    qml.RZ(theta, wires=0)
    qml.RX(env[0] * math.pi, wires=3)
    qml.RY(env[1] * math.pi, wires=5)
    _layer_entangle()
    seed = theta + env[0] + env[1]
    _layer_rotations([math.sin(seed + i) * math.pi for i in range(21)])
    _layer_entangle()
    for _ in range(7):
        qml.Identity(wires=0)
    return sum(qml.expval(qml.PauliZ(w)) for w in range(7)) / 7.0

# qmhs_challenges_inc.py  • PART 2

# === JSON Minify Utility ===
def _json_min(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))

# === Harm Reduction Prompts (Stage 1–3) ===
def stage1_prompt(vec: List[float], s0: Dict[str, Any], s: Settings) -> str:
    data_block = _json_min({"vec": vec, "telemetry": s0})
    return textwrap.dedent(f"""
    Q M H S — STAGE 1 · Cognitive Risk Synthesizer (HARM REDUCTION)
    INPUT: BioVector (vec) and telemetry. Classify risk: Safe / Caution / Overdose.

    Rules:
      θ = L2-norm(vec) × π
      • θ < 1.0        → "Safe"
      • 1.0 ≤ θ < 2.0  → "Caution"
      • θ ≥ 2.0        → "Overdose"
      • toxicityScore > 7        → Overdose
      • fentanylTest == "pos"    → at least Caution
      • recent_overdose == "yes" → escalate 1 tier
      • confidence < {s.confidence_threshold:.2f} → escalate 1 tier
      • noise > 80 & crowding == "high" → escalate

    Output:
      {{
        "theta": <float>,
        "risk": "Safe"|"Caution"|"Overdose",
        "toxicityScore": <int>,
        "modelConfidence": <float>,
        "note": <optional string>
      }}

    INPUT_JSON:
    {data_block}
    """).strip()

def stage2_prompt(r1: Dict[str, Any], s0: Dict[str, Any], s: Settings) -> str:
    tier = r1["risk"]
    n = {"Safe": 1, "Caution": 3, "Overdose": 4}[tier]
    verbs = "Offer, Provide, Guide, Document, Observe, Remind"
    data_block = _json_min({"riskResult": r1, "telemetry": s0})
    return textwrap.dedent(f"""
    Q M H S — STAGE 2 · Action Plan (HARM REDUCTION)
    Return {n} actions and a cooldown integer in minutes.

    Rules:
      • Actions must start with: {verbs}
      • Overdose → include naloxone + call for help
      • Caution → include observation & hydration
      • Safe → grounding or support
      • Cooldown: Safe (30-60), Caution (10-30), Overdose (1-10)

    Output:
      {{
        "actions": [<string> × {n}],
        "cooldown": <int>
      }}

    INPUT_JSON:
    {data_block}
    """).strip()

def stage3_prompt(r1: Dict[str, Any], s: Settings) -> str:
    tone = {
        "Safe": "optimistic-peer",
        "Caution": "reassuring-grounded",
        "Overdose": "urgent-supportive"
    }[r1["risk"]]
    return textwrap.dedent(f"""
    Q M H S — STAGE 3 · Micro-Intervention Script
    Write a ≤ 650 character script for a peer navigator to say aloud.

    Rules:
      • Tone: {tone}
      • Use ONE grounding tool: 4-7-8 breath, 5-sense scan, slow sip, palm press
      • Avoid clinical language. No blame or diagnosis.
      • End with a kind, open-ended question.
      • Output ONLY JSON → {{ "script": "..." }}

    INPUT_JSON:
    {_json_min(r1)}
    """).strip()

# === Relapse Risk Prompts ===
def relapse1_prompt(history: Dict[str, Any], s: Settings) -> str:
    return textwrap.dedent(f"""
    Q M H S — RELAPSE STAGE 1 · Clean User Risk Assessment
    Estimate relapse risk: Low / Moderate / High based on behavior pattern.

    Score 1 point for each:
      • days_clean < 7
      • cravings_today ≥ 6
      • stress ≥ 7
      • sleep_hours < 5
      • supportive_contacts == 0
      • ≥2 exposure_triggers

    Total score:
      0–1 → Low, 2–3 → Moderate, 4+ → High
    Escalate tier if modelConfidence < {s.confidence_threshold:.2f}

    Output JSON:
      {{
        "risk": "Low"|"Moderate"|"High",
        "confidence": <float>
      }}

    INPUT_JSON:
    {_json_min(history)}
    """).strip()

def relapse2_prompt(r1: Dict[str, Any], history: Dict[str, Any], s: Settings) -> str:
    level = r1["risk"]
    n = {"Low": 2, "Moderate": 3, "High": 4}[level]
    verbs = "Plan, Call, Schedule, Remove, Practice, Document"
    return textwrap.dedent(f"""
    Q M H S — RELAPSE STAGE 2 · Mitigation Plan
    Write {n} proactive relapse-prevention steps.

    Rules:
      • Begin each with: {verbs}
      • Keep steps under 11 words
      • Include emotional or social support
      • "High" must include: "Call sober support" and "Remove triggers"
      • follow_up_hours: Low 24–48, Moderate 12–24, High 1–6

    Output JSON:
      {{
        "steps": [<string> × {n}],
        "follow_up_hours": <int>
      }}

    INPUT_JSON:
    {_json_min({"relapseRisk": r1, "history": history})}
    """).strip()

def relapse3_prompt(r1: Dict[str, Any], s: Settings) -> str:
    tone = {
        "Low": "optimistic",
        "Moderate": "steady-reassure",
        "High": "urgent-support"
    }[r1["risk"]]
    tool = "HALT (Hungry, Angry, Lonely, Tired)"
    return textwrap.dedent(f"""
    Q M H S — RELAPSE STAGE 3 · Peer Script
    Compose a ≤ 500 character spoken script to support sobriety.

    Requirements:
      • Tone: {tone}
      • Mention the HALT check (Hungry, Angry, Lonely, Tired)
      • Avoid clinical terms. Be real.
      • End with a question that invites reflection.

    Output JSON:
      {{
        "script": "..."
      }}

    INPUT_JSON:
    {_json_min(r1)}
    """).strip()

# === GUI Snapshot State ===
def gui_snapshot(env: Dict[str, tk.Variable]) -> Dict[str, Any]:
    return {
        "noise": float(env["noise"].get()),
        "lux": float(env["lux"].get()),
        "crowding": env["crowding"].get(),
        "hr": int(env["hr"].get()),
        "spo2": int(env["spo2"].get()),
        "bp": env["bp"].get(),
        "battery_pct": int(env["battery_pct"].get()),
        "naloxone_stock": int(env["naloxone_stock"].get()),
        "fentanylTest": env["fentanylTest"].get(),
        "toxicityScore": int(env["toxicityScore"].get()),
        "recent_overdose": env["recent_overdose"].get(),
    }

# === Tkinter Variable Init ===
gui_snapshot.noise   = tk.DoubleVar(value=55.0)
gui_snapshot.lux     = tk.DoubleVar(value=120.0)
gui_snapshot.crowding= tk.StringVar(value="low")
gui_snapshot.hr      = tk.IntVar(value=78)
gui_snapshot.spo2    = tk.IntVar(value=98)
gui_snapshot.bp      = tk.StringVar(value="118/76")
gui_snapshot.battery_pct = tk.IntVar(value=85)
gui_snapshot.naloxone_stock = tk.IntVar(value=10)
gui_snapshot.fentanylTest   = tk.StringVar(value="neg")
gui_snapshot.toxicityScore  = tk.IntVar(value=2)
gui_snapshot.recent_overdose = tk.StringVar(value="no")

# qmhs_challenges_inc.py  • PART 3

# === QUANTUM ENGINE: 7-Qubit QAdapt ===
DEV = qml.device("default.qubit", wires=7)

class QAdaptEngine:
    def __init__(self, dev: qml.Device, refresh_h: int):
        self.dev = dev
        self.refresh_s = refresh_h * 3600
        self.next_refresh = time.time() + self.refresh_s
        self.layout_gates: List[Tuple[str, Tuple]] = []

    def anneal(self, seed_vec: List[float]) -> None:
        random.seed(hash(tuple(seed_vec)))
        best_score, best_layout = 1e9, None
        for _ in range(64):
            layout = [(random.choice(["RY", "RX", "RZ"]),
                       (random.random() * math.pi, random.randint(0, 6)))
                      for _ in range(7)]
            score = abs(sum(p[0] for _, p in layout) - sum(seed_vec))
            if score < best_score:
                best_score, best_layout = score, layout
        self.layout_gates = best_layout or self.layout_gates
        self.next_refresh = time.time() + self.refresh_s
        LOGGER.info("QAdaptEngine: refreshed layout, score=%.4f", best_score)

    def encode(self, theta: float, env: Tuple[float, float]) -> float:
        if time.time() >= self.next_refresh:
            self.anneal([theta, *env])
        @qml.qnode(self.dev)
        def _dyn():
            qml.RY(theta, wires=0)
            for g, (arg, w) in self.layout_gates:
                getattr(qml, g)(arg, wires=w)
            return qml.expval(qml.PauliZ(0))
        return float(_dyn())

# === SCANNER THREAD ===
class ScannerThread(threading.Thread):
    def __init__(self, cfg: Settings, db: ReportDB, ai: OpenAIClient,
                 status: tk.StringVar, env_vars: Dict[str, tk.Variable]) -> None:
        super().__init__(daemon=True)
        self.cfg, self.db, self.ai, self.status = cfg, db, ai, status
        self.env_vars = env_vars
        self.cap = cv2.VideoCapture(cfg.camera_idx, cv2.CAP_ANY)
        if not self.cap.isOpened():
            raise RuntimeError(f"Unable to open camera index {cfg.camera_idx}")
        self.loop = asyncio.new_event_loop()
        self.stop_ev = threading.Event()
        self.last_overdose_ts: Optional[float] = None
        self.qadapt = QAdaptEngine(DEV, cfg.qadapt_refresh_h)

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.main())

    async def main(self) -> None:
        await self.db.init()
        t0 = 0.0
        try:
            while not self.stop_ev.is_set():
                ok, frame = self.cap.read()
                if ok and (time.time() - t0) >= self.cfg.sampling_interval:
                    t0 = time.time()
                    await self.process(frame)
                await asyncio.sleep(0.05)
        finally:
            await self.db.close()
            self.cap.release()

    def stop(self) -> None:
        self.stop_ev.set()

    async def process(self, frame: np.ndarray) -> None:
        self.status.set("Scanning…")
        env = gui_snapshot(self.env_vars)
        if self.last_overdose_ts and (time.time() - self.last_overdose_ts) < 1800:
            env["recent_overdose"] = "yes"
        s0 = {k: env[k] for k in env}
        vec = [round(float(x), 6) for x in BioVector.from_frame(frame).arr]
        try:
            r1 = json.loads(await self.ai.chat(stage1_prompt(vec, s0, self.cfg), 900))
        except Exception as e:
            LOGGER.error("Stage1 fallback %s", e)
            theta = min(np.linalg.norm(vec), 1.0) * math.pi
            r1 = {"theta": theta, "risk": "Overdose" if theta >= 2 else "Caution", "toxicityScore": env["toxicityScore"]}
        if r1["risk"] == "Overdose":
            self.last_overdose_ts = time.time()
        try:
            r2 = json.loads(await self.ai.chat(stage2_prompt(r1, s0, self.cfg), 850))
        except Exception as e:
            LOGGER.error("Stage2 fallback %s", e)
            r2 = {"actions": ["Provide naloxone", "Observe breathing", "Call 911", "Guide slow sip"], "cooldown": 10}
        try:
            r3 = json.loads(await self.ai.chat(stage3_prompt(r1, self.cfg), 800))
        except Exception as e:
            LOGGER.error("Stage3 fallback %s", e)
            r3 = {"script": "Let's breathe together slowly. You're safe with me."}
        hdr = {
            "ts": s0.get("ts", time.time()),
            "theta": r1["theta"],
            "risk": r1["risk"],
            "actions": r2["actions"],
            "cooldown": r2["cooldown"],
            "toxicityScore": r1.get("toxicityScore"),
            "naloxone_stock": s0.get("naloxone_stock"),
            "confidence": self.cfg.confidence_threshold,
        }
        hdr["digest"] = hashlib.sha256(json.dumps(hdr).encode()).hexdigest()
        q_exp7 = self.qadapt.encode(r1["theta"], (frame.mean() / 255.0, 0.1))
        report = {
            "s0": s0,
            "s1": r1,
            "s2": r2,
            "s3": r3,
            "s4": hdr,
            "q_exp7": float(q_exp7),
        }
        await self.db.save(s0.get("ts", time.time()), report)
        self.status.set(f"Risk {r1['risk']} logged.")
        if self.cfg.mode_autonomous and r1["risk"] == "Overdose":
            mb.showwarning("ALERT", "Overdose tier detected! Provide naloxone, call for help.")
            LOGGER.info("Autonomous alert triggered.")

# === GUI ===
class QMHSApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("QMHS for Challenges Inc – Harm Reduction Risk Scanner")
        self.geometry("960x760")

        self.crypto = AESGCMCrypto(MASTER_KEY)
        self.settings = Settings.load(self.crypto)
        if not os.path.exists(SETTINGS_FILE):
            self.settings.prompt_gui()
            self.settings.save(self.crypto)
        if not self.settings.api_key:
            mb.showerror("Missing API Key", "Please set your OpenAI key in Settings.")
            self.destroy()
            return

        self.status = tk.StringVar(value="Initializing…")
        tk.Label(self, textvariable=self.status, font=("Helvetica", 14)).pack(pady=6)
        env = tk.LabelFrame(self, text="Live Sensor Inputs")
        env.pack(fill="x", padx=8, pady=4)
        def row(lbl, var, col):
            tk.Label(env, text=lbl).grid(row=0, column=col * 2, sticky="e", padx=3)
            tk.Entry(env, textvariable=var, width=8).grid(row=0, column=col * 2 + 1, sticky="w")
        row("Noise dB", gui_snapshot.noise, 0)
        row("Lux", gui_snapshot.lux, 1)
        row("Crowd", gui_snapshot.crowding, 2)
        row("HR", gui_snapshot.hr, 3)
        row("SpO₂", gui_snapshot.spo2, 4)
        row("BP", gui_snapshot.bp, 5)
        row("Battery %", gui_snapshot.battery_pct, 6)
        row("Naloxone Stock", gui_snapshot.naloxone_stock, 7)
        tk.Label(env, text="Fentanyl Test").grid(row=1, column=0, sticky="e")
        tk.OptionMenu(env, gui_snapshot.fentanylTest, "neg", "pos").grid(row=1, column=1, sticky="w")
        row("Toxicity Score", gui_snapshot.toxicityScore, 8)
        tk.Label(env, text="Recent Overdose").grid(row=1, column=2, sticky="e")
        tk.OptionMenu(env, gui_snapshot.recent_overdose, "no", "yes").grid(row=1, column=3, sticky="w")

        btn = tk.Frame(self)
        btn.pack(pady=4)
        tk.Button(btn, text="Settings", command=self.open_settings).grid(row=0, column=0, padx=4)
        tk.Button(btn, text="View Reports", command=self.view_reports).grid(row=0, column=1, padx=4)
        tk.Button(btn, text="📄 Daily Stats", command=self.export_csv).grid(row=0, column=2, padx=4)

        self.text = tk.Text(self, height=25, width=114, wrap="word")
        self.text.pack(padx=6, pady=6)

        self.db = ReportDB(self.settings.db_path, self.crypto)
        self.ai = OpenAIClient(api_key=self.settings.api_key)
        self.scanner = ScannerThread(
            self.settings, self.db, self.ai, self.status, {
                "noise": gui_snapshot.noise,
                "lux": gui_snapshot.lux,
                "crowding": gui_snapshot.crowding,
                "hr": gui_snapshot.hr,
                "spo2": gui_snapshot.spo2,
                "bp": gui_snapshot.bp,
                "battery_pct": gui_snapshot.battery_pct,
                "naloxone_stock": gui_snapshot.naloxone_stock,
                "fentanylTest": gui_snapshot.fentanylTest,
                "toxicityScore": gui_snapshot.toxicityScore,
                "recent_overdose": gui_snapshot.recent_overdose,
            },
        )
        self.scanner.start()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def open_settings(self) -> None:
        self.settings.prompt_gui()
        self.settings.save(self.crypto)
        mb.showinfo("Settings", "Saved. Restart to apply hardware changes.")

    def view_reports(self) -> None:
        rows = asyncio.run(self.db.list_reports())
        if not rows:
            mb.showinfo("Reports", "No reports stored.")
            return
        opts = "\n".join(
            f"{rid} – {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}"
            for rid, ts in rows[:30]
        )
        sel = sd.askstring("Select Report ID", opts)
        self.text.delete("1.0", tk.END)
        if sel:
            try:
                rid = int(sel.split()[0])
                rpt = asyncio.run(self.db.load(rid))
                self.text.insert(tk.END, json.dumps(rpt, indent=2))
            except Exception as e:
                mb.showerror("Error", str(e))

    def export_csv(self) -> None:
        rows = asyncio.run(self.db.list_reports())
        if not rows:
            mb.showinfo("Stats", "No reports to export.")
            return
        fname = f"ci_harmred_stats_{int(time.time())}.csv"
        with open(fname, "w") as f:
            f.write("ts,risk,toxicityScore,naloxone_stock\n")
            for rid, ts in rows:
                rpt = asyncio.run(self.db.load(rid))
                hdr = rpt.get("s4", {})
                f.write(f"{hdr.get('ts')},{hdr.get('risk')},{hdr.get('toxicityScore')},{hdr.get('naloxone_stock')}\n")
        mb.showinfo("Export Complete", f"Stats exported to {fname}")

    def on_close(self) -> None:
        self.scanner.stop()
        self.destroy()

# === MAIN ===
if __name__ == "__main__":
    try:
        QMHSApp().mainloop()
    except KeyboardInterrupt:
        LOGGER.info("Exiting QMHS for Challenges Inc.")
