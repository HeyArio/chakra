#!/usr/bin/env python3
"""Nazarban report service for n8n.
Usage: python3 nazarban_service.py <input_xlsx> <output_pdf> [person_name] [date_str]
"""
import sys, os, json, math, html, statistics
from typing import Optional
import openpyxl, warnings
from openpyxl.utils import column_index_from_string
from playwright.sync_api import sync_playwright
warnings.filterwarnings('ignore')

_HERE = os.path.dirname(os.path.abspath(__file__))
TOTAL_QUESTIONS = 140

def _load_fonts() -> dict:
    path = os.path.join(_HERE, 'fonts_b64.json')
    with open(path) as f:
        return json.load(f)

# ===== scoring engine =====

# Column letters in Questions sheet -> metric key (matches client's SUMPRODUCT refs)
WEIGHT_COLS = {
    'root': 'P', 'sacral': 'Q', 'solar': 'R', 'heart': 'S',
    'throat': 'T', 'thirdeye': 'U', 'crown': 'V',
    'financial': 'W', 'emotional': 'X', 'health': 'Y',
    'receptivity': 'Z', 'intuition': 'AA',
}
CHAKRA_KEYS = ['root', 'sacral', 'solar', 'heart', 'throat', 'thirdeye', 'crown']

# Canonical Persian labels — single source of truth for all chakra/index names
CHAKRA_LABEL = {
    'root': 'ریشه', 'sacral': 'خاجی', 'solar': 'خورشیدی', 'heart': 'قلب',
    'throat': 'گلو', 'thirdeye': 'چشم سوم', 'crown': 'تاج',
}
INDEX_LABEL = {
    'financial': 'مالی', 'emotional': 'عاطفی', 'health': 'سلامت',
    'receptivity': 'دریافت', 'intuition': 'شهود',
}
FA = {**CHAKRA_LABEL, **INDEX_LABEL}

# Chakra -> archetype (from the client's Archetypes sheet)
ARCHETYPE = {
    'root': 'Builder', 'sacral': 'Creator', 'solar': 'Leader', 'heart': 'Healer',
    'throat': 'Messenger', 'thirdeye': 'Visionary', 'crown': 'Mystic',
}
ARCHETYPE_FA = {
    'Builder': 'سازنده', 'Creator': 'خالق', 'Leader': 'رهبر', 'Healer': 'شفادهنده',
    'Messenger': 'پیام‌رسان', 'Visionary': 'بینا', 'Mystic': 'عارف',
}

def level_band(score: Optional[float]) -> tuple[str, str]:
    """Client's IF bands from Calculations!D column."""
    if score is None:
        return ('', '')
    if score < 35:  return ('نیازمند توجه جدی', 'needs_attention')
    if score < 55:  return ('کم‌تعادل',          'low')
    if score < 75:  return ('متعادل نسبی',        'moderate')
    return ('نقطه قوت', 'strength')

def score_workbook(path: str) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True)
    q = wb['Questions']
    r = wb['Responses']

    # answers: row 2..141 -> int 1..4 (blank allowed)
    answers = []
    for row in range(2, TOTAL_QUESTIONS + 2):
        v = r.cell(row, 2).value
        answers.append(int(v) if v not in (None, '') else None)

    # weights per metric (row-aligned with answers)
    weights = {}
    for key, letter in WEIGHT_COLS.items():
        ci = column_index_from_string(letter)
        weights[key] = [q.cell(row, ci).value or 0 for row in range(2, TOTAL_QUESTIONS + 2)]

    def metric_score(key):
        # client's formula:
        # (SUMPRODUCT(ans,w,answered) - SUMPRODUCT(w,answered)) / (3*SUMPRODUCT(w,answered)) *100
        w = weights[key]
        num = 0.0; wsum = 0.0
        for a, wi in zip(answers, w):
            if a is None or wi == 0:
                continue
            num += a * wi
            wsum += wi
        if wsum == 0:
            return None
        raw = (num - wsum) / (3 * wsum) * 100
        return round(raw, 1)

    scores = {k: metric_score(k) for k in WEIGHT_COLS}

    # completion / confidence
    answered = sum(1 for a in answers if a is not None)
    confidence = round(answered / TOTAL_QUESTIONS * 100, 1)

    # overall balance = 100 - population stdev of 7 chakra scores
    chakra_vals = [scores[k] for k in CHAKRA_KEYS if scores[k] is not None]
    balance = round(100 - statistics.pstdev(chakra_vals), 1) if len(chakra_vals) == 7 else None

    # dominant chakra = max of 7
    dominant = max(CHAKRA_KEYS, key=lambda k: (scores[k] if scores[k] is not None else -1))
    arche = ARCHETYPE[dominant]

    # assemble per-metric detail with bands
    detail = {}
    for k, v in scores.items():
        band_fa, band_key = level_band(v)
        detail[k] = {
            'label_fa': FA[k], 'score': v,
            'level_fa': band_fa, 'level': band_key,
        }

    overall = round(sum(chakra_vals) / len(chakra_vals), 1) if chakra_vals else None

    return {
        'metrics': detail,
        'chakras': {k: scores[k] for k in CHAKRA_KEYS},
        'indices': {k: scores[k] for k in ['financial','emotional','health','receptivity','intuition']},
        'overall_score': overall,
        'confidence': confidence,
        'answered': answered,
        'balance': balance,
        'dominant': dominant,
        'dominant_fa': FA[dominant],
        'archetype': arche,
        'archetype_fa': ARCHETYPE_FA[arche],
    }



# ===== report builder =====

CHAKRA_ORDER = ['crown','thirdeye','throat','heart','solar','sacral','root']  # top->bottom (crown high)
CHAKRA_COLOR = {
    'root':'#E5484D','sacral':'#F2802B','solar':'#F5C542','heart':'#4FB477',
    'throat':'#3DA5D9','thirdeye':'#6A6AE3','crown':'#A65FD9',
}
INDEX_ORDER = ['financial','emotional','health','receptivity','intuition']
INDEX_COLOR = '#8b7fb5'

ARCHE_DESC = {  # from client's Archetypes sheet: positive role + shadow
    'Builder':   ('ثبات‌ساز، قابل اعتماد، عملی', 'ترس از تغییر، کنترل‌گری'),
    'Creator':   ('خلاق، جذب‌کننده فرصت، احساسی', 'پراکندگی، وابستگی به لذت'),
    'Leader':    ('قاطع، هدفمند، اثرگذار', 'سلطه‌گری یا ترس از شکست'),
    'Healer':    ('همدل، پیونددهنده، بخشنده', 'فداکاری افراطی، مرز ضعیف'),
    'Messenger': ('شفاف، مذاکره‌گر، روایت‌ساز', 'سکوت یا تندگویی'),
    'Visionary': ('بینش‌مند، استراتژیک، شهودی', 'خیال‌پردازی بدون اقدام'),
    'Mystic':    ('معنادار، الهام‌بخش، متصل', 'فرار از واقعیت، پراکندگی معنوی'),
}
# Interpretation copy per band (from client's Interpretation sheet)
BAND_SUGGEST = {
    'needs_attention': 'تمرین‌های پایه، ثبت احساسات، کاهش فشار و بررسی ریشه‌های رفتاری.',
    'low':             'تمرین‌های کوچک روزانه و ایجاد عادت‌های قابل سنجش.',
    'moderate':        'تمرکز روی تقویت پیوستگی و تبدیل مهارت به عادت.',
    'strength':        'استفاده آگاهانه از این قوت برای حمایت از حوزه‌های ضعیف‌تر.',
}
BAND_LABEL = {
    'needs_attention':'نیازمند توجه جدی','low':'کم‌تعادل',
    'moderate':'متعادل نسبی','strength':'نقطه قوت',
}

def _radar_svg(chakras: dict) -> str:
    """7-axis radar, restyled: dotted rings, gradient fill. Persian labels outside."""
    keys = ['crown','thirdeye','throat','heart','solar','sacral','root']
    cx, cy, R = 210, 205, 150
    n = 7
    def pt(i, r):
        ang = -math.pi/2 + i*2*math.pi/n
        return (cx + r*math.cos(ang), cy + r*math.sin(ang))
    rings = ''
    for frac in (0.25,0.5,0.75,1.0):
        pts = ' '.join(f'{pt(i,R*frac)[0]:.1f},{pt(i,R*frac)[1]:.1f}' for i in range(n))
        rings += f'<polygon points="{pts}" fill="none" stroke="#ffffff" stroke-opacity="0.10" stroke-dasharray="2 4"/>'
    spokes=''; labs=''
    for i,k in enumerate(keys):
        x,y = pt(i,R)
        spokes += f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#ffffff" stroke-opacity="0.08"/>'
        lx,ly = pt(i,R+26)
        labs += f'<text x="{lx:.1f}" y="{ly:.1f}" fill="#b9afce" font-size="13" text-anchor="middle" dominant-baseline="middle">{CHAKRA_LABEL[k]}</text>'
    dpts=[]
    for i,k in enumerate(keys):
        v=(chakras.get(k) or 0)/100
        x,y=pt(i,R*v); dpts.append((x,y))
    poly=' '.join(f'{x:.1f},{y:.1f}' for x,y in dpts)
    dots=''.join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#cbb8ef"/>' for x,y in dpts)
    return f'''<svg viewBox="0 0 420 430" width="330" xmlns="http://www.w3.org/2000/svg">
      <defs><radialGradient id="rg" cx="50%" cy="50%" r="60%">
        <stop offset="0%" stop-color="#7c5cff" stop-opacity="0.55"/>
        <stop offset="100%" stop-color="#7c5cff" stop-opacity="0.08"/>
      </radialGradient></defs>
      {rings}{spokes}
      <polygon points="{poly}" fill="url(#rg)" stroke="#a78bff" stroke-width="1.5"/>
      {dots}{labs}
    </svg>'''

def _spine(chakras: dict) -> str:
    """Signature element: vertical stack of 7 luminous bars = the energy spine."""
    rows=''
    for k in CHAKRA_ORDER:
        v=chakras.get(k) or 0
        c=CHAKRA_COLOR[k]
        rows+=f'''<div class="spine-row">
          <div class="spine-num">{v:.0f}</div>
          <div class="spine-track"><div class="spine-fill" style="width:{v:.0f}%;background:{c};box-shadow:0 0 10px {c}88"></div></div>
          <div class="spine-dot" style="background:{c};box-shadow:0 0 8px {c}"></div>
          <div class="spine-label">{CHAKRA_LABEL[k]}</div>
        </div>'''
    return f'<div class="spine">{rows}</div>'

def _bars(metrics: dict, keys: list, labelmap: dict) -> str:
    out=''
    for k in keys:
        m=metrics[k]; v=m['score'] or 0
        out+=f'''<div class="bar-row">
          <div class="bar-label">{labelmap[k]}</div>
          <div class="bar-track"><div class="bar-fill" style="width:{v:.0f}%"></div></div>
          <div class="bar-val">{v:.0f}</div>
          <div class="bar-tag tag-{m['level']}">{BAND_LABEL[m['level']]}</div>
        </div>'''
    return out

def _interp_rows(metrics: dict, labelmap: dict) -> str:
    """Build per-chakra interpretation rows HTML."""
    parts = []
    for k in CHAKRA_ORDER:
        m = metrics[k]
        score = m['score'] or 0
        color = CHAKRA_COLOR[k]
        parts.append(
            f'<div class="interp-row" style="border-color:{color}">'
            f'<div class="t"><span>{labelmap[k]}</span>'
            f'<span style="color:{color}">{score:.0f}% · {BAND_LABEL[m["level"]]}</span></div>'
            f'<div class="s">{BAND_SUGGEST[m["level"]]}</div>'
            f'</div>'
        )
    return ''.join(parts)

def build_html(data: dict, fonts_b64: dict, person_name: str = '', date_str: str = '') -> str:
    m=data['metrics']
    chak=data['chakras']
    chaklab = CHAKRA_LABEL
    idxlab = INDEX_LABEL
    arche=data['archetype']; arche_fa=data['archetype_fa']
    pos,shadow=ARCHE_DESC.get(arche,('',''))
    dom=data['dominant']; dom_fa=data['dominant_fa']
    overall=data['overall_score'] or 0
    conf=data['confidence']; bal=data['balance'] or 0

    # weakest chakra for the "focus" callout
    weak=min(CHAKRA_ORDER,key=lambda k:(chak.get(k) if chak.get(k) is not None else 999))

    ff=lambda name,w,st='normal':f"@font-face{{font-family:'Vazir';src:url(data:font/ttf;base64,{fonts_b64[name]}) format('truetype');font-weight:{w};font-style:{st};}}"
    fontface=ff('vazir-regular',400)+ff('vazir-medium',500)+ff('vazir-bold',700)+ff('vazir-black',900)

    person_block=f'<span class="meta-name">{html.escape(person_name)}</span>' if person_name else ''

    return f'''<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<style>
{fontface}
*{{margin:0;padding:0;box-sizing:border-box}}
@page{{size:A4;margin:0}}
html,body{{font-family:'Vazir',sans-serif;color:#F4EEFA;background:#0f0b18;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
.page{{width:210mm;height:297mm;padding:14mm 15mm 12mm;background:
   radial-gradient(120% 80% at 80% 0%, #241b38 0%, #160f24 42%, #100b1a 100%);
   position:relative;overflow:hidden;display:flex;flex-direction:column}}
.page::before{{content:'';position:absolute;inset:0;background:
   radial-gradient(50% 40% at 15% 12%, #6a4bd233 0%, transparent 60%);pointer-events:none}}
.page + .page{{page-break-before:always}}

/* header */
.head{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #ffffff1f;padding-bottom:14px;position:relative}}
.brand{{display:flex;flex-direction:column;gap:2px}}
.wordmark{{font-weight:900;font-size:30px;letter-spacing:-.5px;line-height:1;
   background:linear-gradient(92deg,#c9b4ff,#8a6bff 60%,#e08bd0);-webkit-background-clip:text;background-clip:text;color:transparent}}
.latin{{font-size:12px;letter-spacing:5px;color:#8f83aa;text-transform:uppercase;font-weight:500;margin-top:5px}}
.tagline{{font-size:12px;color:#a99dc4;margin-top:6px}}
.head-meta{{text-align:left;font-size:11.5px;color:#8f83aa;line-height:1.9}}
.meta-name{{display:block;color:#e6dcf7;font-size:14px;font-weight:700}}

/* section scaffolding */
.sec{{margin-top:20px}}
.sec-h{{display:flex;align-items:baseline;gap:10px;margin-bottom:12px}}
.sec-n{{font-size:12px;color:#7c6fa0;font-weight:700;letter-spacing:1px}}
.sec-t{{font-size:17px;font-weight:700;color:#efe8fb}}
.sec-line{{flex:1;height:1px;background:#ffffff14;align-self:center;margin-inline-start:6px}}

/* hero: overall + spine */
.hero{{display:grid;grid-template-columns:1.05fr 1fr;gap:24px;margin-top:18px;align-items:center}}
.overall-card{{background:#ffffff08;border:1px solid #ffffff14;border-radius:18px;padding:22px 24px;position:relative}}
.ov-k{{font-size:12.5px;color:#a99dc4}}
.ov-num{{font-size:64px;font-weight:900;line-height:1;margin:6px 0 2px;
   background:linear-gradient(180deg,#d9c8ff,#8f6bff);-webkit-background-clip:text;background-clip:text;color:transparent}}
.ov-sub{{font-size:12.5px;color:#9a8fb0}}
.chips{{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}}
.chip{{font-size:11px;padding:5px 11px;border-radius:20px;background:#ffffff0f;border:1px solid #ffffff1a;color:#c7bce0}}
.chip b{{color:#efe8fb;font-weight:700}}

.spine{{display:flex;flex-direction:column;gap:9px}}
.spine-row{{display:grid;grid-template-columns:26px 1fr 10px 52px;align-items:center;gap:9px}}
.spine-num{{font-size:12px;color:#cabfe0;text-align:center;font-weight:700}}
.spine-track{{height:7px;background:#ffffff10;border-radius:6px;overflow:hidden;direction:ltr}}
.spine-fill{{height:100%;border-radius:6px}}
.spine-dot{{width:9px;height:9px;border-radius:50%}}
.spine-label{{font-size:12.5px;color:#cabfe0}}

/* archetype block */
.arche{{display:grid;grid-template-columns:auto 1fr;gap:18px;background:
   linear-gradient(100deg,#2a1f45 0%,#1b1430 100%);border:1px solid #ffffff14;border-radius:18px;padding:20px 22px;align-items:center}}
.arche-badge{{width:92px;height:92px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;
   background:radial-gradient(circle at 50% 35%,#6a4bd2,#3a2a6e);border:1px solid #ffffff26;box-shadow:0 0 24px #6a4bd255}}
.arche-badge .fa{{font-size:18px;font-weight:900;color:#fff}}
.arche-badge .en{{font-size:9px;letter-spacing:2px;color:#d9ccff;margin-top:2px}}
.arche-body h3{{font-size:15px;color:#efe8fb;margin-bottom:8px}}
.arche-body .row{{font-size:12.5px;color:#b7abd0;margin-bottom:5px;line-height:1.7}}
.arche-body .row b{{color:#e6dcf7}}
.arche-body .k{{color:#8f83aa}}

/* radar + interpretation two-col */
.two{{display:grid;grid-template-columns:340px 1fr;gap:22px;align-items:start}}
.radar-wrap{{background:#ffffff07;border:1px solid #ffffff12;border-radius:18px;padding:12px;display:flex;justify-content:center}}
.legend{{display:flex;flex-direction:column;gap:9px}}

/* bars */
.bar-row{{display:grid;grid-template-columns:74px 1fr 30px 92px;align-items:center;gap:10px;margin-bottom:11px}}
.bar-label{{font-size:12.5px;color:#cabfe0}}
.bar-track{{height:8px;background:#ffffff10;border-radius:6px;overflow:hidden;direction:ltr}}
.bar-fill{{height:100%;border-radius:6px;background:linear-gradient(90deg,#7c5cff,#b46fd0)}}
.bar-val{{font-size:12px;color:#e6dcf7;font-weight:700;text-align:center}}
.bar-tag{{font-size:10.5px;padding:3px 8px;border-radius:12px;text-align:center;white-space:nowrap}}
.tag-strength{{background:#4fb47722;color:#7fe0a6;border:1px solid #4fb47740}}
.tag-moderate{{background:#3da5d922;color:#7fcdf0;border:1px solid #3da5d940}}
.tag-low{{background:#f5c54222;color:#f0d47f;border:1px solid #f5c54240}}
.tag-needs_attention{{background:#e5484d22;color:#f09a9d;border:1px solid #e5484d40}}

/* callouts */
.callouts{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:6px}}
.call{{background:#ffffff08;border:1px solid #ffffff14;border-radius:14px;padding:15px 16px}}
.call .k{{font-size:11px;color:#8f83aa;margin-bottom:6px}}
.call .v{{font-size:14px;color:#efe8fb;font-weight:700;margin-bottom:7px}}
.call .d{{font-size:12px;color:#b0a4c9;line-height:1.75}}

/* interpretation rows */
.interp-row{{border-inline-start:2px solid;padding:8px 12px;margin-bottom:9px;background:#ffffff06;border-radius:0 10px 10px 0}}
.interp-row .t{{font-size:12.5px;color:#e6dcf7;font-weight:700;display:flex;justify-content:space-between}}
.interp-row .s{{font-size:11.5px;color:#a99dc4;margin-top:3px;line-height:1.7}}

/* footer / disclaimer */
.foot{{margin-top:auto;border-top:1px solid #ffffff14;padding-top:11px;display:flex;justify-content:space-between;align-items:center}}
.disc{{font-size:10px;color:#7c7196;line-height:1.7;max-width:70%}}
.foot-brand{{font-size:11px;color:#9a8fb0;font-weight:700}}
</style></head>
<body>
<!-- ===================== PAGE 1 ===================== -->
<div class="page">
  <div class="head">
    <div class="brand">
      <div class="wordmark">نظربان</div>
      <div class="latin">Nazarbanai</div>
      <div class="tagline">تحلیل انرژی و تعادل درونی</div>
    </div>
    <div class="head-meta">
      {person_block}
      <span>گزارش تحلیل فردی</span><br>
      <span>{html.escape(date_str)}</span>
    </div>
  </div>

  <div class="hero">
    <div class="overall-card">
      <div class="ov-k">امتیاز کلی تعادل</div>
      <div class="ov-num">{overall:.0f}<span style="font-size:24px">٪</span></div>
      <div class="ov-sub">میانگین هفت مرکز انرژی شما</div>
      <div class="chips">
        <div class="chip">تعادل کلی <b>{bal:.0f}</b></div>
        <div class="chip">اطمینان داده <b>{conf:.0f}٪</b></div>
        <div class="chip">مرکز غالب <b>{dom_fa}</b></div>
      </div>
    </div>
    {_spine(chak)}
  </div>

  <div class="sec">
    <div class="sec-h"><span class="sec-n">۰۱</span><span class="sec-t">آرکتایپ غالب شما</span><span class="sec-line"></span></div>
    <div class="arche">
      <div class="arche-badge"><div class="fa">{arche_fa}</div><div class="en">{arche.upper()}</div></div>
      <div class="arche-body">
        <h3>الگوی انرژی غالب شما بر پایه مرکز «{dom_fa}» شکل گرفته است</h3>
        <div class="row"><span class="k">نقش مثبت:</span> <b>{pos}</b></div>
        <div class="row"><span class="k">سایه احتمالی:</span> <b>{shadow}</b></div>
      </div>
    </div>
  </div>

  <div class="sec">
    <div class="sec-h"><span class="sec-n">۰۲</span><span class="sec-t">نمای هفت مرکز انرژی</span><span class="sec-line"></span></div>
    <div class="two">
      <div class="radar-wrap">{_radar_svg(chak)}</div>
      <div class="legend">{_bars(m, ['crown','thirdeye','throat','heart','solar','sacral','root'], chaklab)}</div>
    </div>
  </div>

  <div class="foot">
    <div class="disc">این گزارش ابزار خودشناسی و توسعه فردی است و جایگزین تشخیص پزشکی، روان‌پزشکی یا مشاوره مالی نیست. وزن‌ها و امتیازها بر پایه نسخه اولیه محصول محاسبه شده‌اند.</div>
    <div class="foot-brand">nazarbanai.com</div>
  </div>
</div>

<!-- ===================== PAGE 2 ===================== -->
<div class="page">
  <div class="head">
    <div class="brand"><div class="wordmark">نظربان</div><div class="latin">Nazarbanai</div></div>
    <div class="head-meta"><span>ادامه گزارش · ابعاد و مسیر رشد</span></div>
  </div>

  <div class="sec">
    <div class="sec-h"><span class="sec-n">۰۳</span><span class="sec-t">شاخص‌های تکمیلی زندگی</span><span class="sec-line"></span></div>
    <div class="legend">{_bars(m, INDEX_ORDER, idxlab)}</div>
  </div>

  <div class="sec">
    <div class="sec-h"><span class="sec-n">۰۴</span><span class="sec-t">تحلیل کلیدی</span><span class="sec-line"></span></div>
    <div class="callouts">
      <div class="call">
        <div class="k">نقطه قوت غالب</div>
        <div class="v">{dom_fa} · {chak[dom]:.0f}٪</div>
        <div class="d">این مرکز به عنوان منبع اصلی قدرت شما دیده می‌شود. از آن آگاهانه برای حمایت از حوزه‌های ضعیف‌تر استفاده کنید.</div>
      </div>
      <div class="call">
        <div class="k">حوزه نیازمند تمرکز</div>
        <div class="v">{chaklab[weak]} · {chak[weak]:.0f}٪</div>
        <div class="d">{BAND_SUGGEST[m[weak]['level']]}</div>
      </div>
    </div>
  </div>

  <div class="sec">
    <div class="sec-h"><span class="sec-n">۰۵</span><span class="sec-t">تفسیر و پیشنهاد هر مرکز</span><span class="sec-line"></span></div>
    <div>{_interp_rows(m, chaklab)}</div>
  </div>

  <div class="foot">
    <div class="disc">نتایج بر اساس پاسخ‌های ثبت‌شده در پرسشنامه ۱۴۰ سؤالی تولید شده‌اند. برای اعتبار بیشتر، پاسخ‌ها را کامل و صادقانه تکمیل کنید.</div>
    <div class="foot-brand">نظربان · Nazarbanai</div>
  </div>
</div>
</body></html>'''



# ===== renderer =====
def render_html_to_pdf(html_str: str, pdf_path: str) -> None:
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox'])
        pg = b.new_page()
        pg.set_content(html_str, wait_until='networkidle')
        pg.pdf(path=pdf_path, format='A4', print_background=True,
               margin={'top':'0','bottom':'0','left':'0','right':'0'})
        b.close()

# ===== main =====
if __name__ == '__main__':
    in_xlsx = sys.argv[1]
    out_pdf = sys.argv[2]
    person  = sys.argv[3] if len(sys.argv) > 3 else ''
    datestr = sys.argv[4] if len(sys.argv) > 4 else ''
    data  = score_workbook(in_xlsx)
    fonts = _load_fonts()
    html_str = build_html(data, fonts, person_name=person, date_str=datestr)
    render_html_to_pdf(html_str, out_pdf)
    print(json.dumps({'ok': True, 'pdf': out_pdf,
                      'overall': data['overall_score'],
                      'dominant': data['dominant_fa'],
                      'archetype': data['archetype'],
                      'confidence': data['confidence']}, ensure_ascii=False))