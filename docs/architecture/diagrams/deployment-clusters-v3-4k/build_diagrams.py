"""Build editable target-topology SVGs from the accepted project architecture."""

import base64
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
C = {
    "ink": "#172D43",
    "muted": "#667C8C",
    "teal": "#177A75",
    "blue": "#526FA6",
    "amber": "#AE7839",
    "purple": "#8664A0",
    "gray": "#94A5AF",
    "line": "#DCE5E8",
}
COL = {
    "A": "#177A75",
    "B": "#4774A8",
    "C": "#AE7839",
    "D": "#8263A4",
    "E": "#A26483",
    "F": "#5C8192",
    "G": "#6E7860",
    "H": "#6B8C4E",
}
TINT = {
    "A": "#EAF5F0",
    "B": "#EBF1F9",
    "C": "#FAF1E4",
    "D": "#F2EDF8",
    "E": "#F8EDF2",
    "F": "#EBF3F5",
    "G": "#EFF2E9",
    "H": "#EFF5E6",
}
ICONS = {
    "speech": ('<path d="M3 8H1v16h2 M29 8h2v16h-2 M7 13v6 M12 8v16 M17 3v26 M22 8v16 M27 13v6"/>'),
    "browser": (
        '<rect x="2" y="3" width="28" height="21" rx="4"/><path d="M2 9h28 M11 '
        '29h10 M16 24v5 m-4-16-3 3 3 3 m8-6 3 3-3 3"/>'
    ),
    "entry": (
        '<rect x="11" y="10" width="10" height="12" rx="3"/><path d="M2 16h9 M7 '
        '12l4 4-4 4 M21 16h9 M25 12l5 4-5 4 M16 2v8 M12 6l4 4 4-4 M16 22v8"/>'
    ),
    "workbench": (
        '<rect x="3" y="8" width="26" height="21" rx="4"/><path d="M10 8V5a2 2 0 '
        '0 1 2-2h8a2 2 0 0 1 2 2v3 M3 15h26 M12 15v5h8v-5"/>'
    ),
    "realtime": ('<circle cx="16" cy="16" r="14"/><path d="M4 16h5l3-7 6 14 3-7h7"/>'),
    "scheduler": (
        '<rect x="11" y="2" width="10" height="7" rx="2"/><path d="M16 9v7 M5 '
        '16h22 M5 16v6 M16 16v6 M27 16v6"/><rect x="1" y="22" width="8" '
        'height="8" rx="2"/><rect x="12" y="22" width="8" height="8" '
        'rx="2"/><rect x="23" y="22" width="8" height="8" rx="2"/>'
    ),
    "worker": (
        '<rect x="3" y="3" width="17" height="25" rx="3"/><path d="M8 9h7 M8 15h7'
        ' M8 21h4"/><circle cx="24" cy="21" r="6"/><path d="M24 12v3 M24 27v3 M15'
        ' 21h3 M30 21h2"/>'
    ),
    "gpu": (
        '<rect x="7" y="7" width="18" height="18" rx="4"/><rect x="12" y="12" '
        'width="8" height="8" rx="1"/><path d="M11 2v5 M16 2v5 M21 2v5 M11 25v5 '
        'M16 25v5 M21 25v5 M2 11h5 M2 16h5 M2 21h5 M25 11h5 M25 16h5 M25 21h5"/>'
    ),
    "gateway": ('<path d="M16 2 29 7v9c0 7-5 11-13 15C8 27 3 23 3 16V7z M8 16h5l3-6 3 12 3-6h4"/>'),
    "database": (
        '<ellipse cx="16" cy="6" rx="13" ry="5"/><path d="M3 6v19c0 8 26 8 26 0V6'
        ' M3 15c0 8 26 8 26 0"/>'
    ),
    "objects": ('<path d="m16 2 13 7-13 7L3 9z M3 9v15l13 7 13-7V9 M16 16v15 M9 6l13 7"/>'),
    "llm": (
        '<path d="M13 5c-4-3-9 0-8 5-5 1-5 8-1 10-2 5 3 9 7 7 1 3 5 2 '
        "5-1V7c0-4-3-5-3-2 M19 5c4-3 9 0 8 5 5 1 5 8 1 10 2 5-3 9-7 7-1 3-5 2-5-1"
        ' M7 11l4 2 M4 20l6-2 M22 12l5-2 M22 18l6 2"/>'
    ),
}


class Drawing:
    def __init__(self, title, subtitle):
        self.a = []
        self.owners = {}
        self.n = 0
        self.icons = 0
        self.a.append(
            '<svg id="poster" xmlns="http://www.w3.org/2000/svg" width="3840" height="2160" '
            'viewBox="0 0 1920 1080" role="img">'
            f"<title>{html.escape(title)}</title><defs>\n"
            '<linearGradient id="paper" x1="0" y1="0" x2="1" y2="1">'
            '<stop stop-color="#FAFCFD"/><stop offset="1" stop-color="#F2F6F6"/>'
            "</linearGradient>\n"
            '<filter id="shadow" x="-20%" y="-20%" width="140%" height="150%">'
            '<feDropShadow dx="0" dy="4" stdDeviation="7" flood-color="#234150" '
            'flood-opacity=".045"/></filter>\n'
            "<style>text{font-family:Arial,'PingFang SC','Hiragino Sans GB',sans-serif;"
            "text-rendering:geometricPrecision}</style>"
        )
        for k in ["teal", "gray", "amber", "blue", "purple"]:
            self.a.append(
                f'<marker id="arrow-{k}" markerWidth="8" markerHeight="8" refX="6.5" '
                'refY="4" orient="auto-start-reverse"><path d="M1 1 7 4 1 7" fill="none" '
                f'stroke="{C[k]}" stroke-width="1.5" stroke-linejoin="round"/></marker>'
            )
        self.a.append("</defs>")
        self.rect(0, 0, 1920, 1080, "url(#paper)", r=0)
        self.icon("speech", 58, 32, 24)
        self.text(96, 53, "SpeechNexus", 20, bold=700, mw=220)
        self.text(254, 52, "/  TARGET ARCHITECTURE", 12, "muted", mw=650)
        self.text(56, 120, title, 44, bold=650, mw=1740)
        self.text(58, 163, subtitle, 17, "muted", mw=1760)

    def rect(self, x, y, w, h, fill="#FFFFFF", stroke=None, r=14, owner=None, pad=16, shadow=False):
        attrs = ""
        if owner:
            self.owners[owner] = (x, y, w, h, pad)
            attrs = f' id="{owner}" data-layout-container="true" data-inset="{pad}"'
        self.a.append(
            f'<rect{attrs} x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" '
            f'fill="{fill}" stroke="{C.get(stroke, stroke) if stroke else "none"}" '
            'stroke-width="1"' + (' filter="url(#shadow)"' if shadow else "") + "/>"
        )

    def text(self, x, y, s, size=14, col="ink", bold=400, mw=None, owner=None, anchor="start"):
        self.n += 1
        attrs = f' data-check-box="label-{self.n}"'
        if mw is not None:
            attrs += f' data-max-width="{mw}"'
        if owner:
            attrs += f' data-owner="{owner}"'
        self.a.append(
            f'<text{attrs} x="{x}" y="{y}" fill="{C.get(col, col)}" font-size="{size}" '
            f'font-weight="{bold}" text-anchor="{anchor}">{html.escape(s)}</text>'
        )

    def icon(self, kind, x, y, size=24, col="teal", bg=None):
        self.icons += 1
        self.a.append(
            f'<g data-layout-icon="true" data-icon="{kind}" '
            f'transform="translate({x} {y}) scale({size / 32})">'
        )
        if bg:
            self.a.append(f'<rect x="-7" y="-7" width="46" height="46" rx="12" fill="{bg}"/>')
        self.a.append(
            f'<g fill="none" stroke="{C.get(col, col)}" stroke-width="1.7" '
            f'stroke-linejoin="round" stroke-linecap="round">{ICONS[kind]}</g></g>'
        )

    def route(self, d, col="teal", arrow=True, dash=None, network=True, width=1.7, halo=False):
        if halo:
            self.a.append(
                f'<path data-connection-bridge="true" d="{d}" fill="none" '
                'stroke="#FDFEFE" stroke-width="7" stroke-linejoin="round"/>'
            )
        self.a.append(
            f'<path data-route="true" data-network="{str(network).lower()}" d="{d}" '
            f'fill="none" stroke="{C.get(col, col)}" stroke-width="{width}" '
            'stroke-linecap="round" stroke-linejoin="round"'
            + (f' stroke-dasharray="{dash}"' if dash else "")
            + (f' marker-end="url(#arrow-{col})"' if arrow else "")
            + "/>"
        )

    def pill(self, x, y, w, h, label, col="teal", fill="#E7F2ED", owner=None, size=12.5):
        owner = owner or f"pill-{self.n + 1}"
        self.rect(x, y, w, h, fill, r=min(h / 2, 12), owner=owner, pad=7)
        self.text(
            x + w / 2, y + h / 2 + size * 0.34, label, size, col, 600, w - 16, owner, "middle"
        )

    def image(self, key, x, y, w, h):
        payload = base64.b64encode((ROOT / "assets" / f"{key}.png").read_bytes()).decode()
        self.a.append(
            f'<image data-illustration="{key}" x="{x}" y="{y}" width="{w}" height="{h}" '
            f'preserveAspectRatio="xMidYMid meet" href="data:image/png;base64,{payload}"/>'
        )

    def cluster(self, x, y, w, h, name, kind, subtitle, replicas, policy):
        owner = "cluster-" + replicas[0]
        self.rect(x, y, w, h, stroke="line", owner=owner, pad=18, shadow=True)
        self.icon(kind, x + 25, y + 27, 24, bg="#EAF4EF")
        self.text(x + 64, y + 43, name, 16.4, bold=600, mw=w - 86, owner=owner)
        self.text(x + 24, y + 74, subtitle, 12.5, "muted", mw=w - 48, owner=owner)
        for i, label in enumerate(replicas):
            self.pill(x + 24 + i * 66, y + 96, 56, 32, label, size=11.5)
        self.text(x + 24, y + h - 24, policy, 11.9, "teal", mw=w - 48, owner=owner)

    def finish(self, name, expected):
        self.a.append("</svg>")
        (ROOT / (name + ".svg")).write_text("\n".join(self.a) + "\n")
        (ROOT / (name + ".content.json")).write_text(
            json.dumps(
                {
                    "expectedText": expected,
                    "width": 3840,
                    "height": 2160,
                    "textLabels": self.n,
                    "iconCount": self.icons,
                    "sourceKind": "终态目标拓扑，节点与副本数量为示意",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )


def overview():
    d = Drawing(
        "部署集群总览",
        "三种独立扩容维度：服务副本、模型实例副本、计算主机；按角色与数据权威保持边界",
    )
    d.rect(48, 204, 1824, 808, "#FDFEFE", "line", 24)
    d.text(74, 226, "部署网络 · 跨机 TLS · 内容与观测不出网", 13.5, "teal", 600, mw=1000)
    d.text(1846, 226, "拓扑数量为示意，容量由各节点父预算约束", 12, "muted", mw=650, anchor="end")
    # Group frames.
    d.rect(354, 242, 810, 540, "#EDF5F1", "#D7E5DE", 20)
    d.rect(1252, 242, 596, 540, "#F8F2E8", "#E7DAC7", 20)
    d.rect(354, 814, 1494, 174, "#EEF3FA", "#D8E2EF", 20)
    d.text(378, 283, "应用与控制集群", 25, bold=650, mw=380)
    d.text(378, 316, "同角色多副本；不同角色独立扩缩", 14, "muted", mw=600)
    d.pill(760, 265, 214, 32, "macOS  /  Linux", size=13.5)
    d.image("application", 1008, 246, 128, 86)
    d.text(1276, 283, "Linux 计算集群", 25, bold=650, mw=375)
    d.text(1276, 316, "各 HostNode：执行网关 / 节点控制器 / 独立父预算", 12.8, "muted", mw=548)
    d.image("compute", 1722, 246, 102, 69)
    # Network routes use clear gutters, never card text rows.
    d.route("M190 386V426")
    d.text(207, 414, "HTTPS / WSS", 11.8, "teal", mw=120)
    d.route("M308 606H310Q320 606 320 596V345Q320 335 330 335H1016", arrow=False)
    for x in [496, 756, 1016]:
        d.route(f"M{x} 335V354")
    d.route("M638 444H614")
    d.route("M1134 444H1252")
    d.text(1193, 429, "内部执行 / 流", 11.4, "teal", mw=110, anchor="middle")
    d.route("M1134 664H1188Q1198 664 1198 654V444", arrow=False)
    d.route(
        "M496 574V562Q496 552 506 552H1208Q1218 552 1218 562V676Q1218 686 1228 686H1252",
        "gray",
        dash="5 5",
        halo=True,
    )
    d.text(1198, 710, "实例控制", 11.7, "muted", mw=100, anchor="middle")
    d.route(
        "M496 524V530Q496 540 486 540H350Q340 540 340 550V706"
        "Q340 716 330 716H200Q190 716 190 726V744",
        "amber",
    )
    d.route("M720 782V875", "gray")
    d.route("M1350 782V804H720", "gray", arrow=False)
    d.text(734, 796, "事务状态", 12, "muted", mw=130)
    d.route("M1016 782V812H1118V875", "teal", halo=True)
    d.route("M1540 782V812H1118", "teal", arrow=False)
    d.text(1564, 802, "固定版本对象", 11.8, "muted", mw=200)
    # Access and CPU node replicas.
    d.rect(72, 264, 236, 122, stroke="line", owner="clients", pad=18, shadow=True)
    d.icon("browser", 98, 290, 24, bg="#EAF4EF")
    d.text(136, 308, "浏览器与 SDK", 18, bold=600, mw=148, owner="clients")
    d.text(96, 344, "AdminUI / Workbench UI", 12.8, "muted", mw=188, owner="clients")
    d.text(96, 365, "标准 SDK / REST / SSE / WS", 12, "muted", mw=188, owner="clients")
    d.rect(72, 426, 236, 270, stroke="line", owner="entry", pad=18, shadow=True)
    d.icon("entry", 98, 451, 24, bg="#EAF4EF")
    d.text(136, 469, "入口代理集群", 18, bold=600, mw=148, owner="entry")
    d.text(96, 501, "TLS / 同源 / 独立上游", 12.8, "muted", mw=188, owner="entry")
    d.text(96, 524, "REST 均衡 / WS 固定 owner", 12, "teal", mw=188, owner="entry")
    for i, s in enumerate(["Edge-1", "Edge-2", "Edge-n"]):
        d.pill(96, 548 + i * 46, 188, 34, s, fill="#EFF5F3", size=13)
    d.rect(72, 744, 236, 244, "#F9F5EA", "#E5D8C0", 18, owner="cpu", pad=18)
    d.icon("gpu", 98, 770, 24, "amber", bg="#F1E5D0")
    d.text(136, 787, "CPU 节点集群", 17, bold=600, mw=148, owner="cpu")
    d.text(96, 818, "CPU / Linux", 13, "amber", mw=188, owner="cpu")
    d.text(96, 844, "ONNX VAD / 媒体沙箱", 12.7, "muted", mw=188, owner="cpu")
    for i, s in enumerate(["CPU-1", "CPU-2", "CPU-n"]):
        d.pill(96 + i * 66, 865, 56, 32, s, "amber", "#F3E9D7", size=11.5)
    d.text(96, 929, "独立模型副本 / 会话隔离", 12.5, "muted", mw=188, owner="cpu")
    d.text(96, 960, "本节点私有有界临时区", 12, "amber", mw=188, owner="cpu")
    # Six separately scalable application roles.
    d.cluster(
        378,
        354,
        236,
        170,
        "Speech API",
        "speech",
        "公开 REST / SSE",
        ["API-1", "API-2", "API-n"],
        "幂等 / 配额持久共享",
    )
    d.cluster(
        638,
        354,
        236,
        170,
        "Workbench API",
        "workbench",
        "公开协议消费语音底座",
        ["WB-1", "WB-2", "WB-n"],
        "独立表与运行凭据",
    )
    d.cluster(
        898,
        354,
        236,
        170,
        "实时网关",
        "realtime",
        "Realtime Gateway",
        ["RT-1", "RT-2", "RT-n"],
        "固定 owner / stream epoch",
    )
    d.cluster(
        378,
        574,
        236,
        170,
        "Scheduler",
        "scheduler",
        "按逻辑资源池分配 owner",
        ["S-1", "S-2", "S-n"],
        "单池单 owner / fencing",
    )
    d.cluster(
        638,
        574,
        236,
        170,
        "工作台后台",
        "worker",
        "纪要 / 导出 / 副本",
        ["BW-1", "BW-2", "BW-n"],
        "仅认领本模块持久工作",
    )
    d.cluster(
        898,
        574,
        236,
        170,
        "Job Workers",
        "worker",
        "持久 claim / outbox",
        ["W-1", "W-2", "W-n"],
        "attempt / fencing / 去重",
    )
    d.text(378, 768, "副本间依靠 PG / outbox 协调；不共享进程内全局锁", 12, "muted", mw=750)
    # Concrete host containers with independent engine-instance boxes.
    hosts = [
        ("a", 1276, [("A1", "vLLM", "ASR-0.6B", "A"), ("C1", "vLLM-Omni", "CustomVoice", "C")]),
        ("b", 1468, [("A2", "vLLM", "ASR-0.6B", "A"), ("C2", "vLLM-Omni", "CustomVoice", "C")]),
        ("c", 1660, [("B1", "vLLM", "ASR-1.7B", "B"), ("E1", "vLLM-Omni", "VoiceDesign", "E")]),
    ]
    for tag, x, slots in hosts:
        own = "host-" + tag
        d.rect(x, 354, 164, 390, "#FFFDF9", "#E5D8C6", 15, owner=own, pad=12)
        d.icon("gpu", x + 22, 377, 20, "amber")
        d.text(x + 52, 395, "Host-" + tag.upper(), 17, bold=600, mw=94, owner=own)
        d.text(x + 18, 419, "node-" + tag, 11.8, "muted", mw=128, owner=own)
        d.pill(x + 16, 437, 132, 32, "Exec Gateway " + tag.upper(), "amber", "#F4E9D6", size=11.8)
        d.pill(
            x + 16, 482, 132, 32, "Node Controller " + tag.upper(), "amber", "#F4E9D6", size=11.5
        )
        for i, (sid, engine, model, fam) in enumerate(slots):
            y = 543 + i * 96
            box = "engine-" + sid
            d.rect(x + 16, y, 132, 76, TINT[fam], COL[fam], 12, owner=box, pad=10)
            d.text(x + 30, y + 27, engine, 12.3, COL[fam], 600, 104, box)
            d.text(x + 30, y + 54, f"{model} / {sid}", 11.5, "ink", 500, 104, box)
    # Dotted equivalence links are explicitly not runtime traffic.
    for y, fam in [(581, "A"), (677, "C")]:
        d.route(f"M1424 {y}H1484", COL[fam], arrow=False, dash="2 5", network=False, width=1.5)
    d.text(1276, 768, "Host-A / B / C … n；图示代表实例，同机共享父预算", 12, "muted", mw=548)
    # Shared authoritative services: different clustering semantics, not three generic replicas.
    d.text(378, 850, "独立基础服务", 20, bold=600, mw=350)
    d.image("data", 914, 814, 82, 54)
    for name, x, w in [
        ("pg-service", 378, 468),
        ("s3-service", 870, 492),
        ("llm-service", 1386, 438),
    ]:
        d.rect(x, 875, w, 89, stroke="line", owner=name, pad=14)
    d.icon("database", 400, 899, 24, "blue")
    d.text(440, 907, "PostgreSQL · 单写权威", 16, bold=600, mw=380, owner="pg-service")
    d.pill(440, 923, 112, 27, "写主", "blue", "#EAF0F9", size=12)
    d.pill(604, 923, 112, 27, "备副本", "blue", "#EAF0F9", size=12)
    d.route("M552 936H604", "blue")
    d.text(738, 943, "复制", 11.8, "muted", mw=80, owner="pg-service")
    d.icon("objects", 892, 899, 24, "blue")
    d.text(932, 907, "S3 统一端点 / 对象存储集群", 16, bold=600, mw=410, owner="s3-service")
    for i, s in enumerate(["存储-1", "存储-2", "存储-n"]):
        d.pill(932 + i * 116, 923, 98, 27, s, "blue", "#EAF0F9", size=12)
    d.icon("llm", 1408, 899, 24, "purple")
    d.text(1448, 907, "可选内网 LLM 端点", 16, bold=600, mw=350, owner="llm-service")
    d.text(
        1448, 943, "仅工作台消费；不纳管 LLM 模型部署", 12.5, "muted", mw=350, owner="llm-service"
    )
    d.text(
        64,
        1045,
        "完整配方：配额、节点父预算、全部阶段租约及 Job/outbox 同一 PG 事务准入",
        12.5,
        "muted",
        mw=1080,
    )
    d.text(
        1856,
        1045,
        "同色点线：同模型绑定的副本关联，非推理调用或同步通道",
        12,
        "muted",
        mw=720,
        anchor="end",
    )
    d.finish(
        "01-cluster-overview-4k",
        [
            "部署集群总览",
            "Speech API",
            "Workbench API",
            "Realtime Gateway",
            "Scheduler",
            "Job Workers",
            "API-1",
            "API-2",
            "API-n",
            "Host-A",
            "Host-B",
            "Host-C",
            "ASR-0.6B / A1",
            "ASR-0.6B / A2",
            "CustomVoice / C1",
            "CustomVoice / C2",
            "PostgreSQL",
            "单写权威",
            "S3 统一端点",
            "CPU 节点集群",
        ],
    )


MODELS = [
    ("A", "实时 ASR", "Qwen/Qwen3-ASR-0.6B", "vLLM", "GPU", "预加载 / 实时常驻"),
    ("B", "文件 / 复核", "Qwen/Qwen3-ASR-1.7B", "vLLM", "GPU", "文件按策略；实时复核依赖须就绪"),
    (
        "C",
        "预设音色",
        "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "vLLM-Omni",
        "GPU",
        "承担实时合成的绑定常驻",
    ),
    (
        "D",
        "克隆 / 复用",
        "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "vLLM-Omni",
        "GPU",
        "实时常驻；受控创作预算复用",
    ),
    (
        "E",
        "声音设计",
        "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "vLLM-Omni",
        "GPU",
        "按需加载 / 空闲回收",
    ),
    (
        "F",
        "强制对齐",
        "Qwen/Qwen3-ForcedAligner-0.6B",
        "qwen_asr",
        "PyTorch CUDA",
        "独立辅助实例 / 按配方治理",
    ),
    (
        "G",
        "匿名分人",
        "nvidia/Nemotron-3-Diarization",
        "NeMo",
        "PyTorch CUDA",
        "独立辅助实例 / 按配方治理",
    ),
    ("H", "VAD", "Silero VAD ONNX", "ONNX Runtime", "CPU", "CPU 常驻 / 会话状态隔离"),
]


def slot(d, x, y, w, sid, model, engine):
    fam = sid[0]
    owner = "slot-" + sid
    d.rect(x, y, w, 34, TINT[fam], None, 10, owner=owner, pad=8)
    d.text(x + 14, y + 23, sid, 13.3, COL[fam], 700, 38, owner)
    d.text(x + 61, y + 23, model, 13.5, bold=500, mw=w - 198, owner=owner)
    d.text(x + w - 14, y + 23, engine, 12.2, "muted", mw=112, owner=owner, anchor="end")


def models():
    d = Drawing(
        "模型、引擎与实例拓扑",
        "模型绑定固定版本；引擎实例独立标识；同模型副本跨节点扩容；资源池在节点之间聚合容量",
    )
    # Full model identifiers and runtime separation.
    d.rect(64, 230, 912, 676, "#FFFFFF", "line", 20, owner="catalog", pad=20, shadow=True)
    d.text(90, 273, "能力与模型制品", 22, bold=650, mw=560, owner="catalog")
    d.text(742, 273, "推理运行时", 17, "muted", 600, mw=200, owner="catalog")
    for i, (fam, role, model, engine, device, policy) in enumerate(MODELS):
        y = 300 + i * 73
        if i % 2 == 0:
            d.rect(80, y, 880, 69, "#F4F8F9", r=10)
        d.pill(96, y + 19, 28, 30, fam, col=COL[fam], fill=TINT[fam], size=13.5)
        d.text(144, y + 34, role, 14.5, bold=600, mw=118, owner="catalog")
        d.text(278, y + 29, model, 13.9, bold=500, mw=432, owner="catalog")
        d.text(278, y + 54, policy, 12.2, COL[fam], mw=432, owner="catalog")
        d.text(742, y + 29, engine, 14, "blue", 600, mw=208, owner="catalog")
        d.text(742, y + 54, device, 12.3, "muted", mw=208, owner="catalog")
    # Binding, instance and resource-pool identities are not conflated.
    d.rect(1024, 230, 832, 76, "#ECF4F1", "#D6E4DD", 16, owner="binding", pad=16)
    d.text(
        1048, 262, "绑定修订 = 模型与版本 + 引擎 + 固定配置", 18, bold=600, mw=784, owner="binding"
    )
    d.text(
        1048,
        287,
        "副本共享 binding_revision；instance / generation 独立；每实例归属一个 pool",
        12.6,
        "muted",
        mw=784,
        owner="binding",
    )
    d.text(
        1024,
        334,
        "点线仅标识同绑定模型副本；不代表副本之间存在调用或状态同步",
        12.7,
        "muted",
        mw=820,
    )
    # Host A and B intentionally show the same families, different instance IDs.
    for tag, x in [("A", 1024), ("B", 1464)]:
        own = "gpu-" + tag
        d.rect(x, 356, 392, 252, "#FFFFFF", "line", 18, owner=own, pad=16, shadow=True)
        d.icon("gpu", x + 25, 382, 24, "amber", bg="#F5ECDE")
        d.text(x + 64, 396, "Host-" + tag, 20, bold=600, mw=220, owner=own)
        d.pill(x + 294, 374, 74, 28, "Linux", "amber", "#F5ECDE", size=12)
        d.text(
            x + 24,
            428,
            "Execution Gateway " + tag + " · Node Controller " + tag,
            12.3,
            "muted",
            mw=344,
            owner=own,
        )
        idx = "1" if tag == "A" else "2"
        for i, (family, model, engine) in enumerate(
            [
                ("A", "ASR-0.6B", "vLLM"),
                ("C", "CustomVoice", "vLLM-Omni"),
                ("D", "Base", "vLLM-Omni"),
            ]
        ):
            slot(d, x + 24, 440 + i * 44, 344, family + idx, model, engine)
        d.text(x + 24, 587, "共享本节点父预算，不重复计整卡容量", 12, "muted", mw=344, owner=own)
    for y, fam in [(457, "A"), (501, "C"), (545, "D")]:
        d.route(f"M1392 {y}H1488", COL[fam], False, "2 5", False, width=1.7)
    d.rect(1024, 626, 832, 32, "#ECF1F7", None, 10, owner="logical-pools", pad=7)
    d.text(
        1040,
        647,
        "跨节点逻辑池：asr-realtime → A1 / A2；tts-realtime → C1 / C2 / D1 / D2",
        12.4,
        "blue",
        500,
        mw=800,
        owner="logical-pools",
    )
    # Multiple distinct vendor runtimes are separate instances, not one vLLM endpoint.
    d.rect(1024, 674, 392, 306, "#FFFFFF", "line", 18, owner="gpu-c", pad=16, shadow=True)
    d.icon("gpu", 1049, 700, 24, "amber", bg="#F5ECDE")
    d.text(1088, 714, "Host-C", 20, bold=600, mw=220, owner="gpu-c")
    d.pill(1318, 692, 74, 28, "Linux", "amber", "#F5ECDE", size=12)
    d.text(
        1048, 746, "Execution Gateway C · Node Controller C", 12.3, "muted", mw=344, owner="gpu-c"
    )
    for i, args in enumerate(
        [
            ("B1", "ASR-1.7B", "vLLM"),
            ("E1", "VoiceDesign", "vLLM-Omni"),
            ("F1", "ForcedAligner", "qwen_asr"),
            ("G1", "Diarization", "NeMo"),
        ]
    ):
        slot(d, 1048, 760 + i * 45, 344, *args)
    d.text(
        1048, 960, "独立运行时实例；加载与释放受节点预算约束", 12, "muted", mw=344, owner="gpu-c"
    )
    # CPU model replicas keep session state separate; media inspection is not a model.
    d.rect(1464, 674, 392, 306, "#F2F7EC", "#DDE8D2", 18, owner="cpu-pool", pad=16)
    d.text(1488, 714, "CPU 节点集群", 22, bold=600, mw=344, owner="cpu-pool")
    d.text(1488, 739, "ONNX Runtime / 有界媒体沙箱", 13, "muted", mw=344, owner="cpu-pool")
    for idx, x in [("1", 1488), ("2", 1672)]:
        own = "cpu-" + idx
        d.rect(x, 760, 160, 154, "#FFFFFF", "#DDE8D2", 14, owner=own, pad=12)
        d.text(x + 24, 794, "CPU-" + idx, 17, bold=600, mw=112, owner=own)
        d.pill(x + 12, 812, 136, 37, "H" + idx + " · Silero VAD", COL["H"], TINT["H"], size=12.4)
        d.pill(x + 12, 866, 136, 30, "媒体安全检查", "amber", "#F6ECDA", size=12.4)
    d.route("M1636 830H1684", COL["H"], False, "2 5", False, width=1.7)
    d.text(
        1488, 954, "H 副本按会话隔离状态；本地临时缓冲有界", 12.3, "muted", mw=344, owner="cpu-pool"
    )
    # Functional cross-model dependencies, distinct from same-model replicas.
    for own, x, w, title, detail in [
        ("dependency-asr", 64, 440, "实时完整段复核（按配方）", "A 0.6B 预览 → B 1.7B 复核 / 定稿"),
        (
            "dependency-voice",
            528,
            448,
            "声音设计 → 不可变音色 → 复用",
            "E 候选 → D 校验 → VoiceRevision → D 合成",
        ),
    ]:
        d.rect(x, 936, w, 76, "#ECF3F2", None, 14, owner=own, pad=16)
        d.text(x + 24, 965, title, 14, "teal", 600, mw=w - 48, owner=own)
        d.text(x + 24, 992, detail, 14, "ink", 500, mw=w - 48, owner=own)
    d.text(
        1024,
        1008,
        "Host-C 示例：asr-batch → B1；voice-creation → E1；auxiliary → F1 / G1",
        12,
        "muted",
        mw=832,
    )
    d.text(
        1024,
        1031,
        "资源池跨 HostNode 聚合；同节点所有池与实例共享一个父物理预算",
        12.4,
        "muted",
        mw=832,
    )
    d.text(
        64,
        1052,
        "模型共置与副本数量为拓扑示意，不表示固定容量；每次扩容仍受版本、完整配方与父预算约束",
        12.6,
        "muted",
        mw=1750,
    )
    d.finish(
        "02-model-instance-topology-4k",
        ["模型、引擎与实例拓扑"]
        + [x[2] for x in MODELS]
        + [
            "Host-A",
            "Host-B",
            "Host-C",
            "A1",
            "A2",
            "C1",
            "C2",
            "D1",
            "D2",
            "H1 · Silero VAD",
            "H2 · Silero VAD",
            "Execution Gateway A · Node Controller A",
            "副本共享 binding_revision",
            "每实例归属一个 pool",
            "asr-realtime",
            "tts-realtime",
            "voice-creation",
        ],
    )


if __name__ == "__main__":
    overview()
    models()
    (ROOT / "model-catalog.json").write_text(
        json.dumps(
            [
                {
                    "alias": x[0],
                    "capability": x[1],
                    "artifact": x[2],
                    "engine": x[3],
                    "device": x[4],
                    "loading": x[5],
                }
                for x in MODELS
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print("Built two editable SVGs and authoritative model catalog.")
