"""Rising Path Recruiters — dashboard brand theme (frontend-only).

Pure presentation: CSS and HTML snippet builders injected by dashboard.py via
st.markdown(..., unsafe_allow_html=True). This module performs no I/O, no API
calls and imports nothing from the backend; changing it can never alter
business behavior.
"""

from __future__ import annotations

import html

# ---------------------------------------------------------------------------
# Brand tokens (derived from the Rising Path Recruiters logo)
# ---------------------------------------------------------------------------
GOLD = "#F0A824"          # sun / arrow
GOLD_DEEP = "#D97706"     # gradient end, hover
GOLD_TEXT = "#A16207"     # WCAG-safe amber for text on white
BROWN = "#8A5A2B"         # path
BROWN_DEEP = "#6B4423"    # dark path stripe
INK = "#3E2E1E"           # headings (warm brown-black)
BODY = "#57534E"          # body text (stone-600)
BG = "#FAF9F7"            # app background (warm off-white)
CARD = "#FFFFFF"
BORDER = "#E7E0D8"
GREEN = "#15803D"
RED = "#B91C1C"
AMBER_BG = "#FEF3E2"

# Compact logo mark (sun + rising path arrow), inline so it is always crisp.
_LOGO_MARK = """
<svg viewBox="0 0 240 200" width="{w}" height="{h}" role="img" aria-label="Rising Path Recruiters" style="display:block">
  <defs>
    <linearGradient id="rpsun{uid}" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#FFC93C"/><stop offset="1" stop-color="#F59E0B"/>
    </linearGradient>
    <linearGradient id="rparw{uid}" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0" stop-color="#F5A623"/><stop offset="1" stop-color="#FFC93C"/>
    </linearGradient>
  </defs>
  <g fill="#F5A623">
    <path d="M120 8 L124 34 L116 34 Z"/><path d="M158 16 L152 40 L145 37 Z"/>
    <path d="M190 38 L174 56 L169 50 Z"/><path d="M212 72 L189 82 L187 75 Z"/>
    <path d="M82 16 L88 40 L95 37 Z"/><path d="M50 38 L66 56 L71 50 Z"/>
    <path d="M28 72 L51 82 L53 75 Z"/><path d="M20 108 L46 106 L46 113 Z"/>
    <path d="M220 108 L194 106 L194 113 Z"/>
  </g>
  <circle cx="120" cy="112" r="52" fill="url(#rpsun{uid})"/>
  <path d="M22 168 L128 118 L150 118 L44 170 Z" fill="#8A5A2B"/>
  <path d="M62 172 L158 124 L176 124 L92 174 Z" fill="#6B4423"/>
  <path d="M118 156 L188 118 L182 108 L214 100 L208 133 L200 124 L134 160 Z" fill="url(#rparw{uid})"/>
  <path d="M150 174 L196 146 L216 174 Z" fill="#8A5A2B"/>
</svg>
"""

_uid_counter = 0


def logo_svg(size: int = 40) -> str:
    """Inline logo mark. Unique gradient ids so several instances can coexist."""
    global _uid_counter
    _uid_counter += 1
    return _LOGO_MARK.format(w=size, h=int(size * 200 / 240), uid=_uid_counter)


# Small stroke icons (Lucide-style paths) — never emoji.
_ICONS = {
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
    "linkedin": '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 10v7M8 7v.1M12 17v-4a2 2 0 0 1 4 0v4"/>',
    "dice": '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h.01M15 15h.01M12 12h.01"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
    "zap": '<path d="M13 2 4 14h6l-1 8 9-12h-6z"/>',
    "send": '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>',
    "eye": '<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.5"/>',
    "alert": '<path d="M12 3 2.5 20h19z"/><path d="M12 9.5v4.5M12 17.4v.1"/>',
    "gauge": '<path d="M12 15l4-6"/><path d="M20.5 15.5a9 9 0 1 0-17 0"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
}


def _icon(name: str, size: int = 18, stroke: str = "currentColor") -> str:
    return (
        f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
        f'stroke="{stroke}" stroke-width="1.8" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{_ICONS.get(name, _ICONS["target"])}</svg>'
    )


# ---------------------------------------------------------------------------
# Global stylesheet
# ---------------------------------------------------------------------------
THEME_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

:root {{
  --rp-gold: {GOLD};
  --rp-gold-deep: {GOLD_DEEP};
  --rp-gold-text: {GOLD_TEXT};
  --rp-brown: {BROWN};
  --rp-brown-deep: {BROWN_DEEP};
  --rp-ink: {INK};
  --rp-body: {BODY};
  --rp-bg: {BG};
  --rp-card: {CARD};
  --rp-border: {BORDER};
  --rp-radius: 14px;
  --rp-shadow: 0 1px 2px rgba(62,46,30,.05), 0 4px 14px rgba(62,46,30,.06);
  --rp-shadow-lift: 0 2px 4px rgba(62,46,30,.06), 0 12px 28px rgba(62,46,30,.11);
  --rp-ease: cubic-bezier(.22,.61,.36,1);
  --rp-dur: .18s;
}}

html, body, [data-testid="stAppViewContainer"], .stApp {{
  background: var(--rp-bg) !important;
  font-family: 'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif !important;
  color: var(--rp-body);
}}
[data-testid="stHeader"] {{ background: transparent !important; }}
.block-container {{ padding-top: 1.1rem !important; padding-bottom: 2.5rem !important; max-width: 1440px; }}

h1, h2, h3, h4 {{ font-family: 'Plus Jakarta Sans', sans-serif !important; color: var(--rp-ink) !important; letter-spacing: -0.015em; }}
h2 {{ font-size: 1.3rem !important; font-weight: 800 !important; margin-top: .6rem; }}
h3 {{ font-size: 1.06rem !important; font-weight: 700 !important; }}

/* Section headings in the main area: small gold accent + extra breathing room */
[data-testid="stMain"] h2 {{ position: relative; padding-bottom: 8px; margin-bottom: .35rem; }}
[data-testid="stMain"] h2::after {{
  content: ""; position: absolute; left: 0; bottom: 0;
  width: 36px; height: 3px; border-radius: 2px;
  background: linear-gradient(90deg, var(--rp-gold), var(--rp-gold-deep));
}}
[data-testid="stMain"] [data-testid="stVerticalBlock"] > div:has(> [data-testid="stHeading"] h2) {{
  margin-top: .5rem;
}}
/* Empty-state captions read as intentional, not faded leftovers */
[data-testid="stMain"] [data-testid="stCaptionContainer"] p {{
  color: #7A7268 !important; font-size: .86rem !important; line-height: 1.55;
}}
[data-testid="stCaptionContainer"], .stCaption {{ color: #8A8580 !important; }}
a {{ color: var(--rp-gold-text); }}

/* ---------- Sidebar ---------- */
section[data-testid="stSidebar"] {{
  background: #FAF7F2 !important;               /* warm off-white, clearly separate */
  border-right: 1px solid #E3D9CB;
  box-shadow: 1px 0 0 rgba(62,46,30,.03);
  width: 288px !important;
  min-width: 272px !important;
}}
[data-testid="stSidebar"] .block-container {{ padding-top: .8rem !important; }}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: .7rem; }}
[data-testid="stSidebar"] hr {{ border-color: #E3D9CB; }}

/* Readable sidebar text: never pale gray */
[data-testid="stSidebar"] p, [data-testid="stSidebar"] label,
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {{
  color: #2D2D2D !important; font-size: .9rem;
}}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p,
[data-testid="stSidebar"] .stCaption, [data-testid="stSidebar"] small {{
  color: #6B7280 !important; font-size: .78rem !important;
}}
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {{
  color: #2D2416 !important;
}}

/* Uppercase section labels with divider rules */
.rp-side-section {{
  font-size: .68rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase;
  color: var(--rp-brown-deep);
  border-top: 1px solid #E3D9CB;
  padding: 14px 0 2px; margin-top: 6px;
}}
.rp-side-brand + div .rp-side-section, .rp-side-section:first-child {{ border-top: none; margin-top: 0; }}

/* Portal selector as full-width selectable cards */
.st-key-portal_radio div[role="radiogroup"] {{ display: flex; flex-direction: column; gap: 8px; }}
.st-key-portal_radio label[data-baseweb="radio"] {{
  display: flex; align-items: center;
  background: #FFFFFF; border: 1px solid #E3D9CB; border-radius: 11px;
  padding: 10px 14px; margin: 0 !important; width: 100%;
  cursor: pointer;
  transition: border-color var(--rp-dur) var(--rp-ease), background var(--rp-dur) var(--rp-ease),
              box-shadow var(--rp-dur) var(--rp-ease), transform var(--rp-dur) var(--rp-ease);
  box-shadow: 0 1px 2px rgba(62,46,30,.05);
}}
.st-key-portal_radio label[data-baseweb="radio"] > div:first-child {{ display: none !important; }}
.st-key-portal_radio label[data-baseweb="radio"] p {{
  font-size: .92rem !important; font-weight: 700 !important; color: #4B5563 !important;
}}
.st-key-portal_radio label[data-baseweb="radio"]:hover {{
  border-color: var(--rp-gold); background: {AMBER_BG}; transform: translateY(-1px);
  box-shadow: 0 3px 10px rgba(62,46,30,.08);
}}
.st-key-portal_radio label[data-baseweb="radio"]:has(input:checked) {{
  background: linear-gradient(135deg, #FEF3E2 0%, #FDE8C7 100%);
  border: 1.5px solid var(--rp-gold);
  box-shadow: 0 2px 10px rgba(217,119,6,.20), inset 0 1px 0 rgba(255,255,255,.7);
}}
.st-key-portal_radio label[data-baseweb="radio"]:has(input:checked) p {{ color: var(--rp-brown-deep) !important; }}
.st-key-portal_radio label[data-baseweb="radio"]:has(input:checked) p::after {{
  content: "  Active"; font-size: .68rem; font-weight: 800; letter-spacing: .06em;
  text-transform: uppercase; color: var(--rp-gold-text); float: right;
}}

/* Compact, readable sidebar status alerts styled as badges */
[data-testid="stSidebar"] [data-testid="stAlert"] {{
  padding: 8px 12px; border-radius: 10px;
}}
[data-testid="stSidebar"] [data-testid="stAlert"] p {{ font-size: .82rem !important; font-weight: 600; }}
/* Status dot inherits each alert's semantic text color (green/amber/red) */
[data-testid="stSidebar"] [data-testid="stAlert"] p:first-of-type::before {{
  content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: currentColor; opacity: .9; margin-right: 8px; vertical-align: 1px;
}}

/* Sidebar buttons: consistent height, readable in every state */
[data-testid="stSidebar"] .stButton > button {{
  min-height: 40px; font-size: .88rem !important;
}}
[data-testid="stSidebar"] .stButton > button:hover:enabled {{
  background: #FFFDF8 !important;
}}
[data-testid="stSidebar"] .stButton > button:disabled {{
  opacity: 1 !important;
  background: #F1EAE0 !important; border-color: #E3D9CB !important;
  color: #8A8078 !important; cursor: not-allowed;
}}

/* Upload card */
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {{
  background: #FFFDF8 !important; border: 1.5px dashed var(--rp-gold) !important;
  border-radius: 12px; padding: 14px 12px;
  transition: background var(--rp-dur) var(--rp-ease), border-color var(--rp-dur) var(--rp-ease),
              box-shadow var(--rp-dur) var(--rp-ease);
}}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"]:hover {{
  background: {AMBER_BG} !important; border-color: var(--rp-gold-deep) !important;
  box-shadow: 0 2px 10px rgba(217,119,6,.12);
}}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"] div,
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"] span {{
  color: #4B5563 !important;
}}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"] small {{
  color: #6B7280 !important;
}}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button {{
  border-radius: 9px !important; border: 1px solid var(--rp-border) !important;
  background: #FFFFFF !important; color: var(--rp-brown-deep) !important;
  font-weight: 700 !important; min-height: 36px;
}}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button:hover {{
  border-color: var(--rp-gold) !important; color: var(--rp-gold-text) !important;
}}
[data-testid="stSidebar"] [data-testid="stFileUploaderFile"] div {{ color: #2D2D2D !important; }}

/* ---------- Buttons ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  border-radius: 10px !important;
  font-weight: 600 !important;
  font-family: 'Plus Jakarta Sans', sans-serif !important;
  border: 1px solid var(--rp-border) !important;
  color: var(--rp-brown-deep) !important;
  background: #FFFFFF !important;
  box-shadow: 0 1px 2px rgba(62,46,30,.06) !important;
  transition: transform var(--rp-dur) var(--rp-ease), box-shadow var(--rp-dur) var(--rp-ease),
              background var(--rp-dur) var(--rp-ease), border-color var(--rp-dur) var(--rp-ease),
              color var(--rp-dur) var(--rp-ease);
  cursor: pointer;
}}
.stButton > button:hover:enabled, .stDownloadButton > button:hover:enabled, .stFormSubmitButton > button:hover:enabled {{
  transform: translateY(-1px);
  border-color: var(--rp-gold) !important;
  box-shadow: var(--rp-shadow-lift) !important;
  color: var(--rp-gold-text) !important;
}}
.stButton > button:active:enabled, .stFormSubmitButton > button:active:enabled {{
  transform: translateY(0) scale(.985);
}}
.stButton > button:focus-visible, .stDownloadButton > button:focus-visible {{
  outline: 3px solid rgba(240,168,36,.45) !important; outline-offset: 1px;
}}
button[kind="primary"], [data-testid="stBaseButton-primary"] {{
  background: linear-gradient(135deg, #F5A623 0%, {GOLD_DEEP} 100%) !important;
  border: 1px solid {GOLD_DEEP} !important;
  color: #FFFFFF !important;
  box-shadow: 0 2px 8px rgba(217,119,6,.28) !important;
}}
button[kind="primary"]:hover:enabled, [data-testid="stBaseButton-primary"]:hover:enabled {{
  background: linear-gradient(135deg, #F6B03C 0%, #C2680A 100%) !important;
  color: #FFFFFF !important;
  box-shadow: 0 4px 14px rgba(217,119,6,.38) !important;
}}
.stButton > button:disabled {{ opacity: .45; cursor: not-allowed; }}

/* ---------- Metric cards (st.metric) ---------- */
[data-testid="stMetric"] {{
  background: var(--rp-card);
  border: 1px solid var(--rp-border);
  border-radius: var(--rp-radius);
  padding: 14px 16px 12px;
  box-shadow: var(--rp-shadow);
  transition: transform var(--rp-dur) var(--rp-ease), box-shadow var(--rp-dur) var(--rp-ease);
  animation: rp-fade .35s var(--rp-ease);
}}
[data-testid="stMetric"]:hover {{ transform: translateY(-2px); box-shadow: var(--rp-shadow-lift); }}
[data-testid="stMetricLabel"] p {{
  font-size: .74rem !important; font-weight: 600 !important;
  text-transform: uppercase; letter-spacing: .06em; color: #8A8580 !important;
}}
[data-testid="stMetricValue"] {{
  font-family: 'Plus Jakarta Sans', sans-serif !important;
  font-weight: 800 !important; color: var(--rp-ink) !important;
  font-variant-numeric: tabular-nums;
}}

/* ---------- Tables ---------- */
[data-testid="stDataFrame"] {{
  border: 1px solid var(--rp-border);
  border-radius: var(--rp-radius);
  overflow: hidden;
  box-shadow: var(--rp-shadow);
  background: var(--rp-card);
  animation: rp-fade .3s var(--rp-ease);
}}
/* HTML tables (st.table / markdown): branded header + row hover */
[data-testid="stTable"] table, [data-testid="stMarkdownContainer"] table {{
  border-collapse: separate; border-spacing: 0; width: 100%;
  border: 1px solid var(--rp-border); border-radius: 12px; overflow: hidden;
  background: var(--rp-card); box-shadow: var(--rp-shadow);
}}
[data-testid="stTable"] th, [data-testid="stMarkdownContainer"] th {{
  background: #FBF7F0 !important; color: var(--rp-brown-deep) !important;
  font-size: .74rem !important; font-weight: 800 !important;
  text-transform: uppercase; letter-spacing: .06em;
  border-bottom: 1px solid var(--rp-border) !important; padding: 10px 14px !important;
  text-align: left;
}}
[data-testid="stTable"] td, [data-testid="stMarkdownContainer"] td {{
  padding: 9px 14px !important; border-bottom: 1px solid #F1EBE2 !important;
  color: var(--rp-body); font-size: .88rem;
  transition: background var(--rp-dur) var(--rp-ease);
}}
[data-testid="stTable"] tbody tr:hover td,
[data-testid="stMarkdownContainer"] tbody tr:hover td {{ background: #FEF9EF; }}
[data-testid="stTable"] tbody tr:last-child td,
[data-testid="stMarkdownContainer"] tbody tr:last-child td {{ border-bottom: none !important; }}

/* ---------- Alerts (incl. empty states) ---------- */
[data-testid="stAlert"] {{
  border-radius: 12px;
  border: 1px solid var(--rp-border);
  box-shadow: 0 1px 2px rgba(62,46,30,.04);
  animation: rp-fade .3s var(--rp-ease);
}}
[data-testid="stAlert"] p {{ line-height: 1.55; }}

/* ---------- Loading: shimmer skeletons + branded spinner ---------- */
[data-testid="stSkeleton"] {{
  background: linear-gradient(90deg, #F3EDE3 25%, #FBF6EC 45%, #F3EDE3 65%) !important;
  background-size: 400% 100% !important;
  animation: rp-shimmer 1.4s var(--rp-ease) infinite;
  border-radius: 10px !important;
}}
[data-testid="stSpinner"] {{
  background: var(--rp-card); border: 1px solid var(--rp-border);
  border-radius: 12px; padding: 10px 14px; box-shadow: var(--rp-shadow);
}}
[data-testid="stSpinner"] i {{ border-top-color: var(--rp-gold) !important; }}

/* ---------- Inputs ---------- */
[data-baseweb="select"] > div, .stTextInput input, .stNumberInput input, .stTextArea textarea {{
  border-radius: 10px !important;
  border-color: var(--rp-border) !important;
  font-family: 'Plus Jakarta Sans', sans-serif !important;
}}
.stTextInput input:focus, .stNumberInput input:focus, .stTextArea textarea:focus {{
  border-color: var(--rp-gold) !important;
  box-shadow: 0 0 0 3px rgba(240,168,36,.25) !important;
}}

/* ---------- Expanders / status ---------- */
[data-testid="stExpander"] {{
  border: 1px solid var(--rp-border) !important;
  border-radius: var(--rp-radius) !important;
  background: var(--rp-card);
  box-shadow: var(--rp-shadow);
  transition: border-color var(--rp-dur) var(--rp-ease), box-shadow var(--rp-dur) var(--rp-ease);
}}
[data-testid="stExpander"]:hover {{ border-color: #E0CFA9 !important; box-shadow: var(--rp-shadow-lift); }}
[data-testid="stExpander"] summary {{
  font-weight: 600; color: var(--rp-brown-deep);
  transition: background var(--rp-dur) var(--rp-ease);
}}
[data-testid="stExpander"] summary:hover {{ background: #FEFAF2; color: var(--rp-gold-text) !important; }}

/* ---------- Progress: gliding sheen while work is running ---------- */
.stProgress > div > div > div {{ background: #F1EAE0 !important; border-radius: 999px; }}
.stProgress > div > div > div > div {{
  background: linear-gradient(90deg, var(--rp-gold), var(--rp-gold-deep), var(--rp-gold)) !important;
  background-size: 200% 100% !important;
  border-radius: 999px;
  animation: rp-progress 1.8s linear infinite;
}}

/* ---------- File uploader ---------- */
[data-testid="stFileUploaderDropzone"] {{
  border-radius: 12px; border: 1.5px dashed var(--rp-gold) !important;
  background: {AMBER_BG} !important;
}}

/* ---------- Segmented main navigation (st.radio key=main_nav) ---------- */
.st-key-main_nav {{ margin: .2rem 0 .6rem; }}
.st-key-main_nav div[role="radiogroup"] {{
  display: flex; flex-wrap: wrap; gap: 6px;
  background: var(--rp-card);
  border: 1px solid var(--rp-border);
  border-radius: 12px; padding: 6px;
  box-shadow: var(--rp-shadow);
}}
.st-key-main_nav label[data-baseweb="radio"] {{
  border-radius: 9px; padding: 7px 14px; margin: 0 !important;
  cursor: pointer;
  transition: background var(--rp-dur) var(--rp-ease), color var(--rp-dur) var(--rp-ease),
              box-shadow var(--rp-dur) var(--rp-ease), transform var(--rp-dur) var(--rp-ease);
  border: 1px solid transparent;
}}
.st-key-main_nav label[data-baseweb="radio"]:active {{ transform: scale(.97); }}
.st-key-main_nav label[data-baseweb="radio"] > div:first-child {{ display: none !important; }}
.st-key-main_nav label[data-baseweb="radio"] p {{
  font-size: .86rem !important; font-weight: 600 !important; color: var(--rp-body) !important;
}}
.st-key-main_nav label[data-baseweb="radio"]:hover {{ background: {AMBER_BG}; }}
.st-key-main_nav label[data-baseweb="radio"]:has(input:checked) {{
  background: linear-gradient(135deg, #F5A623, {GOLD_DEEP});
  box-shadow: 0 2px 8px rgba(217,119,6,.3);
}}
.st-key-main_nav label[data-baseweb="radio"]:has(input:checked) p {{ color: #FFFFFF !important; }}

/* ---------- Sidebar portal radio ---------- */
[data-testid="stSidebar"] label[data-baseweb="radio"] p {{ font-weight: 600; }}

/* ---------- Brand components ---------- */
.rp-topnav {{
  display: flex; align-items: center; gap: 14px;
  background: var(--rp-card);
  border: 1px solid var(--rp-border);
  border-radius: var(--rp-radius);
  padding: 10px 18px;
  box-shadow: var(--rp-shadow);
  animation: rp-fade .35s ease-out;
}}
.rp-topnav .rp-name {{
  font-weight: 800; font-size: 1.05rem; color: var(--rp-ink); letter-spacing: -.01em; line-height: 1.1;
}}
.rp-topnav .rp-sub {{ font-size: .72rem; font-weight: 600; color: var(--rp-gold-text); text-transform: uppercase; letter-spacing: .08em; }}
.rp-topnav .rp-spacer {{ flex: 1; }}
.rp-chip {{
  display: inline-flex; align-items: center; gap: 6px;
  font-size: .76rem; font-weight: 600;
  border-radius: 999px; padding: 5px 12px;
  border: 1px solid var(--rp-border); color: var(--rp-body); background: #FFFFFF;
  white-space: nowrap;
}}
.rp-chip .rp-dot {{ width: 7px; height: 7px; border-radius: 50%; background: #C7C2BC; }}
.rp-chip.on {{ border-color: #BBE3C5; background: #F0FAF2; color: {GREEN}; }}
.rp-chip.on .rp-dot {{ background: {GREEN}; }}
.rp-chip.gold {{ border-color: #F3D9A4; background: {AMBER_BG}; color: {GOLD_TEXT}; }}
.rp-chip.gold .rp-dot {{ background: var(--rp-gold); }}

.rp-hero {{
  display: flex; align-items: center; gap: 26px;
  background: linear-gradient(120deg, #FFFFFF 0%, #FEF7EA 62%, #FDEED6 100%);
  border: 1px solid #F3E4C8;
  border-radius: 20px;
  padding: 28px 32px;
  margin: 12px 0 8px;
  box-shadow: var(--rp-shadow);
  animation: rp-fade .4s var(--rp-ease);
  position: relative; overflow: hidden;
}}
.rp-hero::after {{
  content: ""; position: absolute; top: -55%; right: -6%;
  width: 360px; height: 360px; border-radius: 50%; pointer-events: none;
  background: radial-gradient(circle, rgba(240,168,36,.16) 0%, rgba(240,168,36,0) 68%);
}}
.rp-hero > div {{ position: relative; }}
.rp-hero h1 {{
  margin: 0 0 6px; font-size: 1.55rem; font-weight: 800; color: var(--rp-ink) !important; letter-spacing: -.02em;
}}
.rp-hero .rp-tag {{
  display: inline-block; font-size: .72rem; font-weight: 700; letter-spacing: .09em; text-transform: uppercase;
  color: var(--rp-gold-text); background: #FFFFFF; border: 1px solid #F3D9A4;
  border-radius: 999px; padding: 4px 12px; margin-bottom: 12px;
}}
.rp-hero p {{ margin: 0; color: var(--rp-body); font-size: .95rem; line-height: 1.6; max-width: 60ch; }}
.rp-hero .rp-logo-wrap {{
  flex-shrink: 0; background: #FFFFFF; border: 1px solid #F3E4C8; border-radius: 16px;
  padding: 14px 18px; box-shadow: var(--rp-shadow);
}}

.rp-kpis {{
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px; margin: 10px 0 4px;
}}
@media (max-width: 1100px) {{ .rp-kpis {{ grid-template-columns: repeat(2, minmax(0,1fr)); }} }}
.rp-kpi {{
  background: var(--rp-card);
  border: 1px solid var(--rp-border);
  border-radius: var(--rp-radius);
  padding: 14px 16px;
  box-shadow: var(--rp-shadow);
  position: relative; overflow: hidden;
  transition: transform var(--rp-dur) var(--rp-ease), box-shadow var(--rp-dur) var(--rp-ease);
  animation: rp-fade .35s var(--rp-ease) backwards;
}}
.rp-kpi:nth-child(2) {{ animation-delay: .04s; }}
.rp-kpi:nth-child(3) {{ animation-delay: .08s; }}
.rp-kpi:nth-child(4) {{ animation-delay: .12s; }}
.rp-kpi:nth-child(5) {{ animation-delay: .16s; }}
.rp-kpi:nth-child(6) {{ animation-delay: .20s; }}
.rp-kpi:nth-child(7) {{ animation-delay: .24s; }}
.rp-kpi:nth-child(8) {{ animation-delay: .28s; }}
.rp-kpi::before {{
  content: ""; position: absolute; inset: 0 0 auto 0; height: 3px;
  background: linear-gradient(90deg, var(--rp-gold), var(--rp-gold-deep));
  opacity: 0; transition: opacity .18s ease;
}}
.rp-kpi:hover {{ transform: translateY(-2px); box-shadow: var(--rp-shadow-lift); }}
.rp-kpi:hover::before {{ opacity: 1; }}
.rp-kpi .rp-kpi-head {{ display: flex; align-items: center; justify-content: space-between; }}
.rp-kpi .rp-kpi-label {{
  font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: #8A8580;
}}
.rp-kpi .rp-kpi-icon {{
  width: 32px; height: 32px; border-radius: 9px;
  display: flex; align-items: center; justify-content: center;
  background: {AMBER_BG}; color: var(--rp-gold-text);
}}
.rp-kpi .rp-kpi-value {{
  font-size: 1.5rem; font-weight: 800; color: var(--rp-ink);
  font-variant-numeric: tabular-nums; line-height: 1.2; margin-top: 4px;
}}
.rp-kpi .rp-kpi-note {{ font-size: .76rem; color: #8A8580; margin-top: 2px; }}
.rp-kpi.good .rp-kpi-icon {{ background: #F0FAF2; color: {GREEN}; }}
.rp-kpi.good .rp-kpi-value {{ color: {GREEN}; }}
.rp-kpi.warn .rp-kpi-icon {{ background: #FDF0F0; color: {RED}; }}
.rp-kpi.muted .rp-kpi-value {{ color: #8A8580; }}

.rp-side-brand {{
  display: flex; align-items: center; gap: 12px;
  padding: 8px 2px 16px; border-bottom: 1px solid #E3D9CB; margin-bottom: 4px;
}}
.rp-side-brand .rp-name {{ font-weight: 800; color: #2D2416; font-size: 1.02rem; line-height: 1.18; letter-spacing: -.01em; }}
.rp-side-brand .rp-sub {{ font-size: .64rem; font-weight: 800; color: var(--rp-gold-text); letter-spacing: .1em; text-transform: uppercase; margin-top: 2px; }}

@keyframes rp-fade {{
  from {{ opacity: 0; transform: translateY(5px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}
@keyframes rp-shimmer {{
  from {{ background-position: 100% 0; }}
  to {{ background-position: 0 0; }}
}}
@keyframes rp-progress {{
  from {{ background-position: 0 0; }}
  to {{ background-position: -200% 0; }}
}}
@media (prefers-reduced-motion: reduce) {{
  * {{ animation: none !important; transition: none !important; }}
}}
@media (max-width: 1440px) {{
  .block-container {{ padding-left: 2rem !important; padding-right: 2rem !important; }}
}}
</style>
"""


def _chip(label: str, on: bool, tone: str = "") -> str:
    cls = "rp-chip on" if on else f"rp-chip {tone}".strip()
    return f'<span class="{cls}"><span class="rp-dot"></span>{html.escape(label)}</span>'


def top_nav(portal: str, linkedin_connected: bool, dice_connected: bool) -> str:
    """Branded top navigation bar: logo, name, portal + connection chips."""
    portal_label = html.escape((portal or "linkedin").title())
    return f"""
<div class="rp-topnav">
  {logo_svg(44)}
  <div>
    <div class="rp-name">Rising Path Recruiters</div>
    <div class="rp-sub">Job Application Platform</div>
  </div>
  <div class="rp-spacer"></div>
  {_chip(f"Active portal: {portal_label}", False, "gold")}
  {_chip("LinkedIn connected" if linkedin_connected else "LinkedIn offline", linkedin_connected)}
  {_chip("Dice connected" if dice_connected else "Dice offline", dice_connected)}
</div>
"""


def welcome_card() -> str:
    return f"""
<div class="rp-hero">
  <div class="rp-logo-wrap">{logo_svg(96)}</div>
  <div>
    <span class="rp-tag">Job Application Platform</span>
    <h1>Welcome to Rising Path Recruiters</h1>
    <p>Automate resume analysis, job matching, LinkedIn Easy Apply, Dice Easy Apply
    and application tracking from one intelligent dashboard.</p>
  </div>
</div>
"""


def kpi_grid(cards: list[dict]) -> str:
    """cards: [{label, value, icon, tone ('', 'good', 'warn', 'muted'), note}]"""
    items = []
    for card in cards:
        tone = card.get("tone") or ""
        note = card.get("note")
        note_html = f'<div class="rp-kpi-note">{html.escape(str(note))}</div>' if note else ""
        items.append(
            f'<div class="rp-kpi {tone}">'
            f'<div class="rp-kpi-head">'
            f'<span class="rp-kpi-label">{html.escape(str(card.get("label", "")))}</span>'
            f'<span class="rp-kpi-icon">{_icon(card.get("icon", "target"))}</span>'
            f"</div>"
            f'<div class="rp-kpi-value">{html.escape(str(card.get("value", "")))}</div>'
            f"{note_html}"
            f"</div>"
        )
    return f'<div class="rp-kpis">{"".join(items)}</div>'


def sidebar_brand() -> str:
    return f"""
<div class="rp-side-brand">
  {logo_svg(48)}
  <div>
    <div class="rp-name">Rising Path<br>Recruiters</div>
    <div class="rp-sub">Job Application Platform</div>
  </div>
</div>
"""


def section_label(text: str) -> str:
    """Small uppercase sidebar section heading with a divider rule."""
    return f'<div class="rp-side-section">{html.escape(text)}</div>'
