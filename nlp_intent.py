"""
Local NLP intent classifier for BlueFors Monitor Slack commands.
TF-IDF (character n-grams) + Logistic Regression — runs entirely on Pi, no API needed.
Supports Chinese and English naturally via character n-grams (no tokenizer needed).
Self-learning: user corrections are saved to nlp_user_examples.jsonl and trigger a retrain.
"""
import re
import json
import logging
from pathlib import Path
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

log = logging.getLogger("monitor")

USER_EXAMPLES_FILE = Path(__file__).parent / "nlp_user_examples.jsonl"

# Human-readable intent labels shown to users
INTENT_LABELS = {
    "plot":             "plot [sensor]",
    "pressure_reading": "pressure reading",
    "pump_status":      "pump status",
    "heater_status":    "heater status",
    "change_threshold": "change threshold",
    "reset_threshold":  "reset threshold",
    "sentinel":         "sentinel on/off",
    "set_mode":         "set mode (cold/idle/auto)",
    "ack":              "acknowledge alert",
    "daily_summary":    "daily summary",
    "help":             "help",
    "status":           "current status",
}

# ── Training data ─────────────────────────────────────────────────────────────
# Each entry: (text, intent)
# Intents: plot | pressure_reading | pump_status | heater_status |
#          change_threshold | reset_threshold | sentinel | set_mode |
#          ack | daily_summary | help | status | unknown

TRAINING_DATA = [
    # ── plot ──────────────────────────────────────────────────────────────────
    ("plot P2",                               "plot"),
    ("plot P5 12h",                           "plot"),
    ("plot MXC",                              "plot"),
    ("plot flow",                             "plot"),
    ("show me a graph of P2",                 "plot"),
    ("P2 pressure chart",                     "plot"),
    ("give me the P5 pressure plot",          "plot"),
    ("MXC temperature graph last hour",       "plot"),
    ("draw a chart of P1",                    "plot"),
    ("P2 last 2 hours",                       "plot"),
    ("show P7 over the last 12 hours",        "plot"),
    ("plot the still temperature",            "plot"),
    ("flow rate graph",                       "plot"),
    ("50K temperature trend",                 "plot"),
    ("P2压力图",                              "plot"),
    ("画一个P2的图",                          "plot"),
    ("P5最近12小时的曲线",                    "plot"),
    ("MXC温度图",                             "plot"),
    ("给我看P1的压力图",                      "plot"),
    ("最近一小时P2压力趋势",                  "plot"),
    ("流量图",                                "plot"),
    ("50K温度曲线",                           "plot"),
    ("P7最近24小时",                          "plot"),
    ("画个图 P2",                             "plot"),
    ("出一个图 MXC",                          "plot"),
    ("P2的变化曲线",                          "plot"),
    ("看看P5的走势",                          "plot"),
    ("压力图P3",                              "plot"),
    ("plot P1 260620_0000 260622_1200",       "plot"),
    ("show still temp from yesterday",        "plot"),

    # ── pressure_reading ──────────────────────────────────────────────────────
    ("pressure reading",                      "pressure_reading"),
    ("show all pressures",                    "pressure_reading"),
    ("what is the pressure",                  "pressure_reading"),
    ("current pressure values",               "pressure_reading"),
    ("check pressure",                        "pressure_reading"),
    ("P1 to P7",                              "pressure_reading"),
    ("give me pressure readings",             "pressure_reading"),
    ("pressure status",                       "pressure_reading"),
    ("all pressure sensors",                  "pressure_reading"),
    ("现在压力是多少",                        "pressure_reading"),
    ("各个压力",                              "pressure_reading"),
    ("压力读数",                              "pressure_reading"),
    ("看看压力",                              "pressure_reading"),
    ("所有压力传感器",                        "pressure_reading"),
    ("P1到P7压力",                            "pressure_reading"),
    ("现在的压力值",                          "pressure_reading"),
    ("查一下压力",                            "pressure_reading"),
    ("压力现在多少",                          "pressure_reading"),
    ("cold cathode on or off",                "pressure_reading"),
    ("cold cathode status",                   "pressure_reading"),

    # ── pump_status ───────────────────────────────────────────────────────────
    ("pump status",                           "pump_status"),
    ("show pump info",                        "pump_status"),
    ("how are the pumps",                     "pump_status"),
    ("B1A pump",                              "pump_status"),
    ("turbo pump status",                     "pump_status"),
    ("compressor status",                     "pump_status"),
    ("R1A pump info",                         "pump_status"),
    ("pump power",                            "pump_status"),
    ("are the pumps running",                 "pump_status"),
    ("check the pumps",                       "pump_status"),
    ("pump speed",                            "pump_status"),
    ("泵的状态",                              "pump_status"),
    ("各个泵怎么样",                          "pump_status"),
    ("查看泵",                                "pump_status"),
    ("泵都开着吗",                            "pump_status"),
    ("B1A泵",                                 "pump_status"),
    ("R1A泵状态",                             "pump_status"),
    ("涡轮泵",                                "pump_status"),
    ("压缩机状态",                            "pump_status"),
    ("泵的功率",                              "pump_status"),
    ("泵在运行吗",                            "pump_status"),
    ("泵信息",                                "pump_status"),
    ("scroll pump",                           "pump_status"),

    # ── heater_status ─────────────────────────────────────────────────────────
    ("heater status",                         "heater_status"),
    ("show heater info",                      "heater_status"),
    ("heat switch status",                    "heater_status"),
    ("still heater",                          "heater_status"),
    ("MXC heater",                            "heater_status"),
    ("are the heaters on",                    "heater_status"),
    ("heater power",                          "heater_status"),
    ("check heaters",                         "heater_status"),
    ("heating status",                        "heater_status"),
    ("加热器状态",                            "heater_status"),
    ("热开关",                                "heater_status"),
    ("加热器信息",                            "heater_status"),
    ("MXC加热器",                             "heater_status"),
    ("Still加热器",                           "heater_status"),
    ("加热器都关了吗",                        "heater_status"),
    ("热开关状态",                            "heater_status"),
    ("加热器开着吗",                          "heater_status"),
    ("查看加热器",                            "heater_status"),

    # ── change_threshold ──────────────────────────────────────────────────────
    ("change MXC to 0.035 for ever",          "change_threshold"),
    ("cold change P2 to 5e-4 for ever",       "change_threshold"),
    ("idle change P5 to 0.001 for 2h",        "change_threshold"),
    ("set MXC threshold to 30mK",             "change_threshold"),
    ("update P2 alert to 0.008",              "change_threshold"),
    ("raise the MXC alarm",                   "change_threshold"),
    ("lower P2 threshold",                    "change_threshold"),
    ("change threshold for MXC",              "change_threshold"),
    ("modify alert level P5",                 "change_threshold"),
    ("把MXC温度警报改成35mK",                 "change_threshold"),
    ("修改MXC阈值",                           "change_threshold"),
    ("提高P2报警阈值",                        "change_threshold"),
    ("降低MXC报警值",                         "change_threshold"),
    ("改一下报警值",                          "change_threshold"),
    ("把P2的阈值改成0.001",                   "change_threshold"),
    ("cold模式下MXC阈值改成35mK",             "change_threshold"),
    ("idle模式P2阈值",                        "change_threshold"),
    ("修改cold模式报警",                      "change_threshold"),
    ("阈值改一下",                            "change_threshold"),
    ("MXC报警太灵敏了改高一点",               "change_threshold"),

    # ── reset_threshold ───────────────────────────────────────────────────────
    ("reset MXC",                             "reset_threshold"),
    ("reset P2",                              "reset_threshold"),
    ("cold reset P2",                         "reset_threshold"),
    ("idle reset P5",                         "reset_threshold"),
    ("restore default threshold",             "reset_threshold"),
    ("reset MXC threshold",                   "reset_threshold"),
    ("restore P2 to default",                 "reset_threshold"),
    ("reset all thresholds",                  "reset_threshold"),
    ("恢复MXC默认",                           "reset_threshold"),
    ("重置阈值",                              "reset_threshold"),
    ("恢复默认报警值",                        "reset_threshold"),
    ("把MXC改回默认",                         "reset_threshold"),
    ("P2重置",                                "reset_threshold"),
    ("恢复P2",                                "reset_threshold"),
    ("阈值恢复默认",                          "reset_threshold"),

    # ── sentinel ──────────────────────────────────────────────────────────────
    ("sentinel on",                           "sentinel"),
    ("sentinel off",                          "sentinel"),
    ("turn on sentinel",                      "sentinel"),
    ("turn off sentinel",                     "sentinel"),
    ("enable CS2 alerts",                     "sentinel"),
    ("disable CS2 alerts",                    "sentinel"),
    ("pause alerts",                          "sentinel"),
    ("resume alerts",                         "sentinel"),
    ("stop forwarding alerts",                "sentinel"),
    ("开启预警",                              "sentinel"),
    ("关闭预警",                              "sentinel"),
    ("暂停预警",                              "sentinel"),
    ("恢复预警",                              "sentinel"),
    ("CS2报警开",                             "sentinel"),
    ("CS2报警关",                             "sentinel"),
    ("停止预警转发",                          "sentinel"),
    ("打开sentinel",                          "sentinel"),
    ("关掉sentinel",                          "sentinel"),

    # ── set_mode ──────────────────────────────────────────────────────────────
    ("set mode cold",                         "set_mode"),
    ("set mode idle",                         "set_mode"),
    ("set mode auto",                         "set_mode"),
    ("switch to cold mode",                   "set_mode"),
    ("force cold mode",                       "set_mode"),
    ("go to idle mode",                       "set_mode"),
    ("mode cold",                             "set_mode"),
    ("mode idle",                             "set_mode"),
    ("auto mode",                             "set_mode"),
    ("manually set mode",                     "set_mode"),
    ("切换到cold模式",                        "set_mode"),
    ("设为室温模式",                          "set_mode"),
    ("改成idle",                              "set_mode"),
    ("自动模式",                              "set_mode"),
    ("强制cold",                              "set_mode"),
    ("手动设置模式",                          "set_mode"),
    ("切换模式",                              "set_mode"),
    ("改成cold",                              "set_mode"),
    ("设置成auto",                            "set_mode"),
    ("mode设为cold",                          "set_mode"),

    # ── ack ───────────────────────────────────────────────────────────────────
    ("ack",                                   "ack"),
    ("acknowledged",                          "ack"),
    ("silence all alerts",                    "ack"),
    ("mute all",                              "ack"),
    ("quiet",                                 "ack"),
    ("got it silence",                        "ack"),
    ("stop all alerts for now",               "ack"),
    ("知道了",                                "ack"),
    ("收到 静音",                             "ack"),
    ("静音所有报警",                          "ack"),
    ("关掉所有报警",                          "ack"),
    ("先不用报警了",                          "ack"),
    ("全部静音",                              "ack"),

    # ── daily_summary ─────────────────────────────────────────────────────────
    ("daily summary",                         "daily_summary"),
    ("summary",                               "daily_summary"),
    ("give me a summary",                     "daily_summary"),
    ("12 hour summary",                       "daily_summary"),
    ("send report",                           "daily_summary"),
    ("what happened today",                   "daily_summary"),
    ("status report",                         "daily_summary"),
    ("发一个总结",                            "daily_summary"),
    ("今天的总结",                            "daily_summary"),
    ("12小时总结",                            "daily_summary"),
    ("发报告",                                "daily_summary"),
    ("最近发生了什么",                        "daily_summary"),
    ("总结一下",                              "daily_summary"),
    ("给我发一个报告",                        "daily_summary"),

    # ── help ──────────────────────────────────────────────────────────────────
    ("help",                                  "help"),
    ("show commands",                         "help"),
    ("what can you do",                       "help"),
    ("list commands",                         "help"),
    ("how do I use this",                     "help"),
    ("commands",                              "help"),
    ("帮助",                                  "help"),
    ("怎么用",                                "help"),
    ("有什么命令",                            "help"),
    ("命令列表",                              "help"),
    ("你能做什么",                            "help"),
    ("使用说明",                              "help"),

    # ── status ────────────────────────────────────────────────────────────────
    ("status",                                "status"),
    ("show status",                           "status"),
    ("current status",                        "status"),
    ("what is the current mode",              "status"),
    ("fridge status",                         "status"),
    ("system status",                         "status"),
    ("show mode",                             "status"),
    ("系统状态",                              "status"),
    ("当前状态",                              "status"),
    ("现在什么模式",                          "status"),
    ("状态怎么样",                            "status"),
    ("查看状态",                              "status"),
    ("看看状态",                              "status"),
    ("mode",                                  "status"),
]

# ── Classifier ────────────────────────────────────────────────────────────────

def _load_user_examples():
    """Load user-corrected examples from disk."""
    if not USER_EXAMPLES_FILE.exists():
        return []
    examples = []
    try:
        with open(USER_EXAMPLES_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                examples.append((obj["text"], obj["intent"]))
    except Exception as e:
        log.warning(f"NLP: failed to load user examples: {e}")
    return examples


def add_example(text: str, intent: str, source: str = "user") -> None:
    """Persist a new labeled example and invalidate the classifier cache for rebuild."""
    try:
        with open(USER_EXAMPLES_FILE, "a") as f:
            f.write(json.dumps({"text": text, "intent": intent, "source": source}) + "\n")
        _classifier._pipeline = None  # trigger rebuild on next call
        log.info(f"NLP: learned '{text[:60]}' → {intent} [{source}]")
    except Exception as e:
        log.error(f"NLP: failed to save example: {e}")


class IntentClassifier:
    def __init__(self):
        self._pipeline = None

    def _build(self):
        all_data = list(TRAINING_DATA) + _load_user_examples()
        texts, labels = zip(*all_data)
        pipe = Pipeline([
            ("tfidf", TfidfVectorizer(
                analyzer="char_wb",
                ngram_range=(2, 4),
                min_df=1,
                sublinear_tf=True,
            )),
            ("clf", LogisticRegression(
                max_iter=1000, C=2.0,
                class_weight="balanced",
                solver="lbfgs",
            )),
        ])
        pipe.fit(texts, labels)
        user_count = len(all_data) - len(TRAINING_DATA)
        log.info(f"NLP: trained on {len(all_data)} examples ({user_count} user-learned)")
        return pipe

    def predict(self, text: str):
        if self._pipeline is None:
            self._pipeline = self._build()
        proba = self._pipeline.predict_proba([text])[0]
        idx   = proba.argmax()
        classes = self._pipeline.classes_
        return classes[idx], float(proba[idx])


_classifier = IntentClassifier()


# ── Entity extraction ─────────────────────────────────────────────────────────

# Sensor keywords — use (?<!\d) / (?!\d) instead of \b so Chinese chars don't break matching
_SENSOR_PATTERNS = [
    (r"mxc[\s_]?far|mxcfar",                   "MXC_TEMPERATURE_FAR"),
    (r"mxc(?![a-z0-9])|mxc温度",               "MXC_TEMPERATURE"),
    (r"still(?![a-z0-9])|still温度",           "STILL_TEMPERATURE"),
    (r"(?<![0-9])4k(?![a-z0-9])|4k板",         "4K_TEMPERATURE"),
    (r"50k(?![a-z0-9])|50k板",                 "50K_TEMPERATURE"),
    (r"b1a(?![a-z0-9])",                        "B1A_TEMPERATURE"),
    (r"(?<![0-9a-z])b2(?![a-z0-9])",           "B2_TEMPERATURE"),
    (r"(?<![0-9a-z])p1(?!\d)|p1压力",          "P1_PRESSURE"),
    (r"(?<![0-9a-z])p2(?!\d)|p2压力",          "P2_PRESSURE"),
    (r"(?<![0-9a-z])p3(?!\d)",                  "P3_PRESSURE"),
    (r"(?<![0-9a-z])p4(?!\d)",                  "P4_PRESSURE"),
    (r"(?<![0-9a-z])p5(?!\d)|p5压力",          "P5_PRESSURE"),
    (r"(?<![0-9a-z])p6(?!\d)",                  "P6_PRESSURE"),
    (r"(?<![0-9a-z])p7(?!\d)",                  "P7_PRESSURE"),
    (r"flow(?![a-z0-9])|流量|he流量",          "FLOW_VALUE"),
]

def _extract_sensor(text: str):
    t = text.lower()
    for pattern, mapping in _SENSOR_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            return mapping
    return None


def _extract_all_sensors(text: str) -> list:
    """Return all sensor mappings found in text (for multi-sensor plot)."""
    t = text.lower()
    found = []
    for pattern, mapping in _SENSOR_PATTERNS:
        if mapping not in found and re.search(pattern, t, re.IGNORECASE):
            found.append(mapping)
    return found


_CHINESE_HOURS = {"一": 1, "两": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "十二": 12, "二十四": 24}

def _extract_duration_minutes(text: str):
    """Return duration in minutes, or None if not found."""
    t = text.lower()
    # Nh or Nhours
    m = re.search(r"(\d+(?:\.\d+)?)\s*h(?:our)?s?", t)
    if m:
        return int(float(m.group(1)) * 60)
    # Nmin
    m = re.search(r"(\d+)\s*min(?:utes?)?", t)
    if m:
        return int(m.group(1))
    # Chinese N小时
    m = re.search(r"(\d+)\s*小时", t)
    if m:
        return int(m.group(1)) * 60
    # Chinese 一/两/...小时
    for word, val in _CHINESE_HOURS.items():
        if word + "小时" in text:
            return val * 60
    # 半小时
    if "半小时" in text:
        return 30
    # Chinese N分钟
    m = re.search(r"(\d+)\s*分钟", t)
    if m:
        return int(m.group(1))
    return None


def _extract_time_range(text: str):
    """Return (start_str, end_str) if YYMMDD_HHMM format found, else None."""
    times = re.findall(r"\d{6}_\d{4}", text)
    if len(times) >= 2:
        return times[0], times[1]
    return None


def _extract_float_value(text: str):
    """Extract a numeric threshold value, handling mK/mbar unit conversion."""
    # mK → K
    m = re.search(r"([\d.]+(?:e[+-]?\d+)?)\s*mk\b", text, re.IGNORECASE)
    if m:
        return float(m.group(1)) / 1000

    # mbar → bar
    m = re.search(r"([\d.]+(?:e[+-]?\d+)?)\s*mbar\b", text, re.IGNORECASE)
    if m:
        return float(m.group(1)) / 1000

    # "to <value>" or "改成/到 <value>" — most explicit patterns first
    m = re.search(r"\bto\s+([\d.]+(?:e[+-]?\d+)?)", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:改成|变成|设为|调为|调到|设置为|到)\s*([\d.]+(?:e[+-]?\d+)?)", text)
    if m:
        return float(m.group(1))

    # Scientific notation not attached to a sensor name (e.g. 5e-4, 1e-6)
    m = re.search(r"(?<![pP])(\d+\.?\d*[eE][+-]?\d+)", text)
    if m:
        return float(m.group(1))

    # Multi-digit float or decimal (avoid single digit from P1-P9)
    m = re.search(r"(?<![pPbB])(\d{2,}\.?\d*|\d+\.\d+)", text)
    if m:
        return float(m.group(1))

    return None


def _extract_mode(text: str):
    t = text.lower()
    if any(w in t for w in ["cold", "低温", "冷"]):
        return "cold"
    if any(w in t for w in ["idle", "室温", "暖", "warm"]):
        return "idle"
    if any(w in t for w in ["auto", "自动"]):
        return "auto"
    return None


def _extract_mode_prefix(text: str):
    """For change/reset: detect explicit cold/idle prefix."""
    t = text.lower().strip()
    if t.startswith("cold ") or "cold模式" in t or "cold change" in t or "cold reset" in t:
        return "cold"
    if t.startswith("idle ") or "idle模式" in t or "idle change" in t or "idle reset" in t:
        return "idle"
    return None


def _extract_on_off(text: str):
    t = text.lower()
    if any(w in t for w in ["on", "开", "启", "resume", "enable", "打开"]):
        return "on"
    if any(w in t for w in ["off", "关", "停", "pause", "disable", "暂停", "关掉"]):
        return "off"
    return None


def extract_entities(text: str, intent: str) -> dict:
    entities = {}
    if intent == "plot":
        all_s = _extract_all_sensors(text)
        entities["sensor"]   = all_s[0] if all_s else None   # first sensor (primary)
        entities["sensors"]  = all_s                          # all sensors (multi-plot)
        entities["minutes"]  = _extract_duration_minutes(text) or 30
        entities["range"]    = _extract_time_range(text)

    elif intent in ("change_threshold", "reset_threshold"):
        entities["sensor"]       = _extract_sensor(text)
        entities["mode_prefix"]  = _extract_mode_prefix(text)
        if intent == "change_threshold":
            entities["value"]    = _extract_float_value(
                re.sub(r"(cold|idle)\s+change", "", text, flags=re.IGNORECASE))
            entities["minutes"]  = _extract_duration_minutes(text)  # None = permanent

    elif intent == "sentinel":
        entities["on_off"] = _extract_on_off(text)

    elif intent == "set_mode":
        entities["mode"] = _extract_mode(text)

    return entities


# ── Main entry point ──────────────────────────────────────────────────────────

def classify_command(text: str):
    """
    Returns (intent, entities, confidence).
    intent: one of the INTENT strings or 'unknown'
    entities: dict with extracted parameters
    confidence: 0.0–1.0
    """
    intent, confidence = _classifier.predict(text)
    entities = extract_entities(text, intent)
    return intent, entities, confidence
