"""Daricomma question collector — পেজ থেকে সব ডেটা নেয়।

Class / Subject / Chapter / Topic প্রশ্ন-পেজের filter box থেকে পড়ে।
প্রশ্নের ধরন নির্বিশেষে প্রশ্ন, options, answer, solution, images এবং math
content সংগ্রহের চেষ্টা করে। MathJax/KaTeX/MathML-এর source-ও text-এ রাখে।

স্বয়ংক্রিয় loop: একটি শ্রেণির সব বিষয় → প্রতি বিষয়ের সব অধ্যায় → প্রতি অধ্যায়ের সব Topic
(একটার পর একটা; আগের Topic-এর chip কেটে তবে পরেরটা বাছা হয়)। নেওয়া প্রশ্ন আবার নেওয়া হয় না;
নতুন প্রশ্ন আগের শেষ নম্বরের পর থেকে যোগ হয়। খালি Topic/অধ্যায় শুধু বাদ দিয়ে এগিয়ে যায়।

config.json-এর ঐচ্ছিক সেটিং (সব ডিফল্ট ঠিক আছে):
  auto_loop (true), start_from_current (false), collect_when_no_topics (false),
  ignore_done_markers (false), chip_close_selector (".sc-hrXxpv.eaMJSE .close-icon")
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urljoin, parse_qs, unquote


def N(text: str) -> str:
    """বাংলা য় (U+09DF) ও য+় দুইভাবেই লেখা হয়; তুলনার আগে একই রূপে আনি।"""
    return unicodedata.normalize("NFD", text or "")


ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
PROFILE = ROOT / "browser_profile"  # Login state আছে: কাউকে শেয়ার করবেন না।
OUT = ROOT / CFG.get("output_dir", "output")
OUT.mkdir(parents=True, exist_ok=True)
DB_PATH = ROOT / CFG.get("database", "output/daricomma_questions.sqlite3")
JSON_PATH = ROOT / CFG.get("json_export", "output/daricomma_questions.json")
IMG_DIR = OUT / "images"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA_VERSION = "4"
SHOW_BUTTON_TEXT = re.compile(r"Show\s+Answer\s*&\s*Solution", re.I)


# ----------------------------------------------------------------- database

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    # Schema বদলালে পুরনো ফাইল মুছে না ফেলে নাম বদলে রেখে নতুন করে শুরু।
    if DB_PATH.exists():
        with db() as con:
            has_q = con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='questions'"
            ).fetchone()
            has_m = con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='meta'"
            ).fetchone()
            ver = con.execute(
                "SELECT value FROM meta WHERE key='schema'"
            ).fetchone() if has_m else None

        if has_q and (not ver or ver[0] != SCHEMA_VERSION):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = DB_PATH.with_name(
                f"{DB_PATH.stem}.old-{stamp}{DB_PATH.suffix}"
            )
            DB_PATH.replace(backup)
            if JSON_PATH.exists():
                JSON_PATH.replace(
                    JSON_PATH.with_name(
                        f"{JSON_PATH.stem}.old-{stamp}{JSON_PATH.suffix}"
                    )
                )
            print(f"ℹ পুরনো ডেটাবেস সরিয়ে রাখা হলো: {backup.name}")

    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY, class_name TEXT, subject TEXT, chapter_no TEXT,
            chapter TEXT, topic TEXT, qtype TEXT, question_text TEXT,
            options_json TEXT, answer_text TEXT, solution_text TEXT,
            images_json TEXT, seq INTEGER)""")
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))


def init_progress() -> None:
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS progress (
            topic_key TEXT, page_no TEXT, PRIMARY KEY (topic_key, page_no))""")


def load_progress() -> set[tuple[str, str]]:
    with db() as con:
        return set(con.execute("SELECT topic_key, page_no FROM progress").fetchall())


def mark_page_done(key: str, page_no: str) -> None:
    with db() as con:
        con.execute("INSERT OR IGNORE INTO progress VALUES (?,?)", (key, page_no))


def slim(d: dict[str, Any]) -> dict[str, Any]:
    d = dict(d)

    def jl(key: str) -> list:
        try:
            return json.loads(d.pop(key, None) or "[]")
        except Exception:
            return []

    return {
        "id": d.get("id") or "",
        "class": d.get("class_name") or d.get("class") or "",
        "subject": d.get("subject") or "",
        "chapter_no": d.get("chapter_no") or "",
        "chapter": d.get("chapter") or "",
        "topic": d.get("topic") or "",
        "qtype": d.get("qtype") or "",
        "question_text": d.get("question_text") or "",
        "options": jl("options_json") or d.get("options") or [],
        "answer": d.get("answer_text") or d.get("answer") or "",
        "solution": d.get("solution_text") or d.get("solution") or "",
        "images": jl("images_json") or d.get("images") or [],
        "seq": int(d.get("seq") or 0),
    }


def load_existing() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    with db() as con:
        con.row_factory = sqlite3.Row
        for row in con.execute("SELECT * FROM questions ORDER BY seq, rowid").fetchall():
            d = dict(row)
            if d.get("id"):
                records[d["id"]] = slim(d)
    return records


def save_record(r: dict[str, Any]) -> None:
    with db() as con:
        con.execute("""INSERT OR REPLACE INTO questions
            (id, class_name, subject, chapter_no, chapter, topic, qtype,
             question_text, options_json, answer_text, solution_text, images_json, seq)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            r["id"], r["class"], r["subject"], r["chapter_no"], r["chapter"],
            r["topic"], r["qtype"], r["question_text"],
            json.dumps(r["options"], ensure_ascii=False), r["answer"],
            r["solution"], json.dumps(r.get("images", []), ensure_ascii=False),
            int(r.get("seq") or 0)))


def build_nested(records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    tree: dict[tuple, dict[str, list]] = {}
    for r in sorted(records.values(), key=lambda x: x.get("seq") or 0):
        tree.setdefault((r["class"], r["subject"], r["chapter"]), {}) \
            .setdefault(r["topic"], []).append(r)

    out = []
    for (cls, sub, chap), topics in tree.items():
        out.append({
            "class": cls, "sub": sub, "Chap": chap,
            "topics": [{
                "topic": topic,
                "questions": [{
                    "no": i + 1,
                    "type": q["qtype"],
                    "question text": q["question_text"],
                    "images": q.get("images", []),
                    "options": q["options"],
                    "answer": q["answer"],
                    "solution": q["solution"],
                } for i, q in enumerate(qs)],
            } for topic, qs in topics.items()],
        })
    return out


def export_json(records: dict[str, dict[str, Any]]) -> None:
    tmp = JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(build_nested(records), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(JSON_PATH)


# ------------------------------------------------------- read filter values

READ_FILTERS_JS = r"""() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const N = s => s.normalize('NFD').replace(/[*:]/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
  const raw = {
    'বিষয়': 'subject', 'subject': 'subject',
    'অধ্যায়': 'chapter', 'chapter': 'chapter',
    'টপিক': 'topic', 'topic': 'topic',
    'শ্রেণি': 'class', 'শ্রেণী': 'class', 'ক্লাস': 'class', 'class': 'class'
  };
  const names = {};
  for (const k of Object.keys(raw)) names[N(k)] = raw[k];
  const out = {};
  for (const lab of document.querySelectorAll('label')) {
    const key = names[N(lab.textContent)];
    if (!key || out[key] !== undefined) continue;
    let ctl = lab.htmlFor ? document.getElementById(lab.htmlFor) : null;
    if (!ctl && lab.parentElement) ctl = lab.parentElement.querySelector('input, select, textarea');
    out[key] = ctl ? clean(ctl.value) : '';
  }
  out.url = location.href;
  return out;
}"""


# ------------------------------------------------------------- card extract

EXTRACT_CARD_JS = r"""(root, qtype) => {
  const clean = s => (s || '')
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n[ \t]+/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  const abs = u => {
    try { return new URL(u, location.href).href; }
    catch (e) { return u || ''; }
  };

  // MathML → LaTeX. চিহ্ন (বাংলা সংখ্যা, +, −, ×, ÷, = ইত্যাদি) Unicode-এ হুবহু থাকে;
  // শুধু গঠন (ভগ্নাংশ, ঘাত, মূল, সাবস্ক্রিপ্ট) LaTeX-এ লেখা হয়।
  const mmlToTex = node => {
    if (!node) return '';
    if (node.nodeType === 3) return node.textContent;
    const tag = (node.localName || '').toLowerCase();
    const kids = [...node.children];
    const k = i => kids[i] ? mmlToTex(kids[i]) : '';
    const all = () => kids.map(mmlToTex).join('');
    const wrap = t => '{' + t + '}';
    switch (tag) {
      case 'math': case 'mrow': case 'mstyle': case 'semantics':
      case 'mpadded': case 'mrow ': return all();
      case 'annotation': case 'annotation-xml': return '';
      case 'mi': case 'mn': case 'mo': return (node.textContent || '').trim();
      case 'mtext': return '\\text{' + (node.textContent || '').trim() + '}';
      case 'mspace': return ' ';
      case 'mfrac': return '\\frac' + wrap(k(0)) + wrap(k(1));
      case 'msup': return wrap(k(0)) + '^' + wrap(k(1));
      case 'msub': return wrap(k(0)) + '_' + wrap(k(1));
      case 'msubsup': return wrap(k(0)) + '_' + wrap(k(1)) + '^' + wrap(k(2));
      case 'munder': return wrap(k(0)) + '_' + wrap(k(1));
      case 'mover': return wrap(k(0)) + '^' + wrap(k(1));
      case 'munderover': return wrap(k(0)) + '_' + wrap(k(1)) + '^' + wrap(k(2));
      case 'msqrt': return '\\sqrt' + wrap(all());
      case 'mroot': return '\\sqrt[' + k(1) + ']' + wrap(k(0));
      case 'mfenced': return (node.getAttribute('open') || '(') + all() +
                             (node.getAttribute('close') || ')');
      default: return all() || (node.textContent || '').trim();
    }
  };

  // গাণিতিক অক্ষর/গ্রিক চিহ্ন → নিয়মিত রূপ ও LaTeX command (𝜋 → \pi, − ইত্যাদি অপরিবর্তিত).
  const GREEK = {
    'π':'\\pi','Π':'\\Pi','θ':'\\theta','Θ':'\\Theta','α':'\\alpha','β':'\\beta',
    'γ':'\\gamma','Γ':'\\Gamma','δ':'\\delta','Δ':'\\Delta','ε':'\\epsilon','λ':'\\lambda',
    'Λ':'\\Lambda','μ':'\\mu','σ':'\\sigma','Σ':'\\Sigma','ω':'\\omega','Ω':'\\Omega',
    'φ':'\\phi','Φ':'\\Phi','ρ':'\\rho','τ':'\\tau','η':'\\eta','ζ':'\\zeta','ξ':'\\xi',
    'ψ':'\\psi','χ':'\\chi','∞':'\\infty'
  };
  const texClean = t => (t || '')
    .replace(/[\u{1D400}-\u{1D7FF}]/gu, ch => ch.normalize('NFKD'))
    .replace(/[π-ωΠΘΓΔΛΣΩΦΨ∞]/g, ch => GREEK[ch] || ch)
    .replace(/[πΠθΘαβγΓδΔελΛμσΣωΩφΦρτηζξψχ∞]/g, ch => GREEK[ch] || ch);

  const inlineTex = t => ' \\(' + texClean(t).trim() + '\\) ';
  const displayTex = t => ' \\[' + texClean(t).trim() + '\\] ';

  // DOM clone থেকে readable text বানায়; সব গাণিতিক সূত্র LaTeX-এ রূপান্তর করে.
  const richText = el => {
    if (!el) return '';
    const box = el.cloneNode(true);

    // ১) MathJax 2: source থাকে <script type="math/tex">; পাশের rendered অংশ বাদ.
    box.querySelectorAll('script[type*="math/tex"]').forEach(e => {
      const display = /mode=display/.test(e.type || '');
      let prev = e.previousElementSibling;
      if (prev && /MathJax/.test(prev.className || '')) prev.remove();
      e.replaceWith(document.createTextNode(
        display ? displayTex(e.textContent) : inlineTex(e.textContent)));
    });

    // ২) MathJax 3: assistive MathML থেকে LaTeX (SVG/HTML rendering-এর উপর নির্ভর নয়).
    box.querySelectorAll('mjx-container').forEach(e => {
      const m = e.querySelector('mjx-assistive-mml math, math');
      const tex = m ? mmlToTex(m).trim() : '';
      const display = e.getAttribute('display') === 'true';
      const body = tex || (e.textContent || '').replace(/\s+/g, ' ').trim();
      if (!body) { e.replaceWith(document.createTextNode(' ')); return; }
      e.replaceWith(document.createTextNode(display ? displayTex(body) : inlineTex(body)));
    });

    // ৩) KaTeX: annotation-এ মূল LaTeX থাকে.
    box.querySelectorAll('.katex').forEach(e => {
      const a = e.querySelector('annotation[encoding="application/x-tex"]');
      const ml = e.querySelector('math');
      const body = a ? a.textContent : (ml ? mmlToTex(ml) : (e.textContent || ''));
      const display = e.closest('.katex-display') !== null;
      e.replaceWith(document.createTextNode(display ? displayTex(body) : inlineTex(body)));
    });

    // ৪) বাকি MathML (সরাসরি <math>).
    box.querySelectorAll('math').forEach(e => {
      const tex = mmlToTex(e).trim();
      const display = e.getAttribute('display') === 'block';
      e.replaceWith(document.createTextNode(
        tex ? (display ? displayTex(tex) : inlineTex(tex)) : ' '));
    });

    // ৫) এখন বাকি noise (SVG আইকন, বোতাম ইত্যাদি) বাদ।
    box.querySelectorAll(
      'button, svg, style, noscript, [role="tooltip"], ' +
      '[class*="Tooltip"], [class*="Popover"], .MathJax_Preview'
    ).forEach(e => e.remove());

    // ৬) ছবি ও লাইন-বিরতি.
    box.querySelectorAll('img').forEach(e => {
      e.replaceWith(document.createTextNode(' [ছবি] '));
    });
    box.querySelectorAll('br').forEach(e => {
      e.replaceWith(document.createTextNode('\n'));
    });
    box.querySelectorAll('div,p,li,tr,h1,h2,h3,h4,h5,h6').forEach(e => {
      e.appendChild(document.createTextNode('\n'));
    });

    return clean(box.textContent || '');
  };

  const txtOf = el => richText(el);
  const NOISE = /^(বোর্ড বই|Show\s+Answer\s*&\s*Solution|Show\s+Answer)$/i;
  const BAD = 'button, svg, [role="button"], [role="tooltip"], [class*="Tooltip"], [class*="Popover"]';

  const srcOf = im => abs(
    im.currentSrc || im.getAttribute('src') || im.dataset.src ||
    im.dataset.original || im.getAttribute('data-lazy-src') || ''
  );
  const validImg = im => {
    if (im.closest(BAD)) return false;
    const r = im.getBoundingClientRect();
    const w = r.width || im.naturalWidth || 0;
    const h = r.height || im.naturalHeight || 0;
    if (w < 20 || h < 20) return false;
    if (/icon|logo|avatar|emoji/i.test((im.alt || '') + ' ' + (im.className || ''))) return false;
    return !!srcOf(im);
  };

  // Common site-specific options plus generic option-like rows.
  const knownOptionEls = [
    ...root.querySelectorAll('.sc-ityoPf.fONsQU .sc-jxNRli.fhRjBD')
  ];
  const optionLabel = /^(?:[ক-ঘA-Da-d][.)।:]|\(?[ক-ঘA-Da-d]\)?[.)।:]?)\s*/;
  const genericOptionEls = [...root.querySelectorAll(
    '[class*="option"], [class*="Option"], [class*="answer-choice"], [role="listitem"]'
  )].filter(el => {
    const t = txtOf(el);
    return t && optionLabel.test(t) && t.length < 1500;
  });

  // ----- class নামের উপর নির্ভর না করে: ক./খ./A./B. লেবেল ধরে অপশন খোঁজা -----
  const leafMatch = re => [...root.querySelectorAll('*')].find(e =>
    e.children.length === 0 && re.test((e.textContent || '').replace(/\s+/g, ' ').trim()));
  const stopEl = leafMatch(/^Show\s+Answer\s*&\s*Solution$/i) ||
                 leafMatch(/^(?:Answer|সঠিক\s*উত্তর|উত্তর)\s*[:ঃ]?$/i);
  const beforeStop = n => !stopEl || (n.compareDocumentPosition(stopEl) & Node.DOCUMENT_POSITION_FOLLOWING);
  const LBL = /^(?:[ক-ঘ]|[A-Da-d])\s*[.)।:]/;
  const anchors = [];
  {
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (tw.nextNode()) {
      const n = tw.currentNode, t = (n.nodeValue || '').trim();
      if (!t || !LBL.test(t)) continue;
      const par = n.parentElement;
      if (!par || par.closest('.katex, mjx-container, math, svg, button, .MathJax')) continue;
      if (!beforeStop(n)) continue;
      anchors.push(n);
    }
  }
  const structOptEls = [];
  if (anchors.length >= 2 && /^(?:[কA]|a)/.test((anchors[0].nodeValue || '').trim())) {
    for (const a of anchors) {
      let el = a.parentElement;
      while (el.parentElement && el.parentElement !== root && root.contains(el.parentElement)) {
        const up = el.parentElement;
        if (anchors.filter(x => up.contains(x)).length !== 1) break;
        el = up;
      }
      if (!structOptEls.includes(el)) structOptEls.push(el);
    }
  }

  let optEls = structOptEls.length >= 2 ? structOptEls
             : (knownOptionEls.length ? knownOptionEls : genericOptionEls);
  // Nested selectors একই option একাধিকবার দিলে বাইরের/ভিতরের duplicate বাদ.
  optEls = optEls.filter((el, i, arr) =>
    !arr.some((other, j) => i !== j && other.contains(el) &&
      arr[j].parentElement !== el)
  );

  const optionText = el => {
    // অপশন এক লাইনে: লেবেল ও সূত্র আলাদা লাইনে ভাঙলে জোড়া লাগানো হয়।
    const t = txtOf(el).replace(/\s*\n\s*/g, ' ').replace(/\s{2,}/g, ' ').trim();
    if (t) return t;
    return [...el.querySelectorAll('img')].some(validImg) ? '[ছবি]' : '';
  };
  let options = optEls.map(optionText).filter(Boolean);

  // Text lines থেকে label-যুক্ত options বের করার fallback.
  const fullLines = (root.innerText || '')
    .split('\n').map(x => x.replace(/\s+/g, ' ').trim()).filter(Boolean);
  const labelOnly = /^[ক-ঘA-Da-d][.)।:]?$/;
  const labeledLine = /^[ক-ঘA-Da-d][.)।:]\s*\S/;
  const fbOptions = [];
  for (let i = 0; i < fullLines.length; i++) {
    if (labelOnly.test(fullLines[i]) && fullLines[i + 1] &&
        !labelOnly.test(fullLines[i + 1])) {
      fbOptions.push(fullLines[i] + ' ' + fullLines[i + 1]);
      i++;
    } else if (labeledLine.test(fullLines[i])) {
      fbOptions.push(fullLines[i]);
    }
  }
  if (!options.length && fbOptions.length >= 2) options = fbOptions;

  // Answer / Solution label এবং তার কাছের content.
  const spans = [...root.querySelectorAll('span, label, strong, b')];
  const findLabel = re => spans.find(s =>
    re.test((s.textContent || '').replace(/\s+/g, ' ').trim()) &&
    (s.textContent || '').trim().length < 30
  );
  const answerLabel = findLabel(/^(?:সঠিক\s*)?উত্তর\s*[:ঃ]?$/i) ||
                      findLabel(/^Answer\s*[:ঃ]?$/i);
  const solutionLabel = findLabel(/^(?:সমাধান|ব্যাখ্যা)\s*[:ঃ]?$/i) ||
                        findLabel(/^Solution\s*[:ঃ]?$/i);

  const NEXT_LABEL = /\n?\s*(?:সমাধান|ব্যাখ্যা|Solution|Show\s+Answer|বোর্ড বই)\s*[:ঃ]?/i;
  const getAfterLabel = (label, stopRe) => {
    if (!label) return '';
    let node = label.parentElement;
    for (let depth = 0; node && root.contains(node) && depth < 4; depth++) {
      const plain = richText(node).replace(/\u00a0/g, ' ');
      const idx = plain.search(stopRe);
      if (idx >= 0) {
        const rest = clean(plain.slice(idx).replace(stopRe, '').split(NEXT_LABEL)[0]);
        if (rest) return rest;
      }
      node = node.parentElement;
    }
    return '';
  };

  let answer = getAfterLabel(answerLabel, /(?:উত্তর|Answer)\s*[:ঃ]\s*/i);
  if (options.length) answer = answer.replace(/^(?:[A-Da-d]|[ক-ঘ])\s*[.)।:]\s*/, '');
  answer = answer.replace(/\s*\n\s*/g, ' ').trim();
  let solution = '';

  // Known solution containers first, then label-based traversal.
  const solBox = root.querySelector(
    '.sc-dogGTU.ceXubr, [class*="solution"], [class*="Solution"], [class*="explanation"]'
  );
  const FOOTER_LINE = /^(বোর্ড বই|Show\s+Answer|Answer\s*[:ঃ]|উত্তর\s*[:ঃ]|Solution\s*[:ঃ]|সমাধান\s*[:ঃ]|ব্যাখ্যা\s*[:ঃ])/i;
  if (solBox) solution = txtOf(solBox);
  if (!solution && solutionLabel) {
    // "Solution:" লাইনের পরের element-ই solution-এর লেখা।
    let node = solutionLabel.parentElement;
    for (let d = 0; node && root.contains(node) && d < 4 && !solution; d++) {
      const sib = node.nextElementSibling;
      if (sib && root.contains(sib)) {
        const t = txtOf(sib);
        if (t && !FOOTER_LINE.test(t)) solution = t;
      }
      node = node.parentElement;
    }
  }
  if (!solution && solutionLabel) {
    solution = getAfterLabel(solutionLabel, /(?:সমাধান|ব্যাখ্যা|Solution)\s*[:ঃ]\s*/i);
  }
  solution = solution.replace(/^(?:সমাধান|ব্যাখ্যা|Solution)\s*[:ঃ]\s*/i, '');

  // Question boundary: cut before options where possible, otherwise before answer panel.
  const answerPanel = answerLabel?.parentElement || solutionLabel?.parentElement || null;
  const firstOption = optEls[0] || null;
  const cloneBefore = cut => {
    if (!cut || cut === root || !root.contains(cut)) return '';
    try {
      const range = document.createRange();
      range.selectNodeContents(root);
      range.setEndBefore(cut);
      const fragment = range.cloneContents();
      const box = document.createElement('div');
      box.appendChild(fragment);
      box.querySelectorAll(BAD).forEach(e => e.remove());
      return richText(box);
    } catch (e) {
      return '';
    }
  };

  let question = '';
  if (firstOption) question = cloneBefore(firstOption);
  if (!question && answerPanel) question = cloneBefore(answerPanel);

  // Fallback: options/answer panel না পাওয়া গেলে পুরো কার্ডের rich text নেওয়া হয়
  // (math রূপান্তরসহ), তারপর footer/answer-এর আগে কাটা হয়।
  if (!question) {
    const fullRich = richText(root).split('\n').map(x => x.trim()).filter(Boolean);
    let end = fullRich.findIndex(l =>
      /^(?:Show\s+Answer|Answer\s*[:ঃ]|উত্তর\s*[:ঃ]|Solution\s*[:ঃ]|সমাধান\s*[:ঃ]|বোর্ড বই)/i.test(l)
    );
    if (end < 0) end = fullRich.length;
    question = clean(fullRich.slice(0, end).filter(l => !NOISE.test(l)).join('\n'));
  }

  // ----- প্রশ্নের শুদ্ধকরণ: footer ও option-এর লেখা কোনোভাবেই প্রশ্নে যাবে না -----
  {
    let qLines = clean(question).split('\n').map(x => x.trim()).filter(Boolean);
    const footerAt = qLines.findIndex(l => FOOTER_LINE.test(l));
    if (footerAt >= 0) qLines = qLines.slice(0, footerAt);
    if (qtype !== 'cq') {
      const optAt = qLines.findIndex((l, i) => i > 0 && (labelOnly.test(l) || labeledLine.test(l)));
      if (optAt > 0) qLines = qLines.slice(0, optAt);
    }
    const optSet = new Set(options.map(o => o.replace(/\s+/g, ' ').trim()));
    qLines = qLines.filter(l => !optSet.has(l.replace(/\s+/g, ' ').trim()));
    question = clean(qLines.join('\n'));
  }

  // Options-কে question text-এ ঢুকে যাওয়া থেকে রক্ষা; options না থাকলে
  // প্রশ্নের বাকি সব লেখা (CQ-র ক, খ, গ, ঘ sub-question-সহ) অক্ষত রাখা হয়.
  if (options.length && firstOption && !question) {
    question = cloneBefore(firstOption);
  }

  // Images: question / option / solution বিভাগ অনুযায়ী.
  const images = [];
  const allImages = [...root.querySelectorAll('img')];
  for (const im of allImages) {
    if (!validImg(im)) continue;
    let where = 'question';
    const oi = optEls.findIndex(o => o.contains(im));
    if (oi >= 0) {
      where = 'option ' + (oi + 1);
    } else if ((solBox && solBox.contains(im)) ||
        (solutionLabel &&
         (solutionLabel.compareDocumentPosition(im) &
          Node.DOCUMENT_POSITION_FOLLOWING))) {
      where = 'solution';
    }
    images.push({src: srcOf(im), where});
  }

  return {
    question: clean(question),
    options,
    answer: clean(answer),
    solution: clean(solution),
    hasPanel: !!(answerLabel || solutionLabel || solBox),
    images
  };
}"""


# প্রতিটি কার্ডে Show Answer-এর পাশের আইকন বোতাম চিহ্নিত.
MARK_ICONS_JS = r"""root => {
  root.querySelectorAll('[data-dc-ic]').forEach(e => e.removeAttribute('data-dc-ic'));
  const txt = e => (e.textContent || '').replace(/\s+/g, ' ').trim();
  const show = [...root.querySelectorAll('span,button,div,a,p')]
      .find(e => e.children.length === 0 && /^Show\s+Answer\s*&\s*Solution$/i.test(txt(e)));
  const clickable = el => el.closest('button, [role="button"], a') || el.parentElement || el;
  const out = [];
  const push = el => { if (el && !out.includes(el)) out.push(el); };

  root.querySelectorAll('[class*="info-circle"], [class*="icon-info"], [aria-label*="info" i], [title*="info" i], [data-testid*="info" i]')
      .forEach(e => push(clickable(e)));

  const sr = show ? show.getBoundingClientRect() : null;
  const icons = [];
  for (const s of root.querySelectorAll('svg')) {
    const el = clickable(s);
    if (!el || icons.includes(el) || out.includes(el)) continue;
    if (show && (show.contains(el) || el.contains(show))) continue;
    if (txt(el).length > 1) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 18 || r.width > 100 || r.height < 18 || r.height > 100) continue;
    if (sr && (Math.abs(r.top - sr.top) > 70 || r.left < sr.right - 2)) continue;
    icons.push(el);
  }
  icons.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
  icons.forEach(push);
  out.slice(0, 4).forEach((e, i) => e.setAttribute('data-dc-ic', String(i)));
  return Math.min(out.length, 4);
}"""


READ_INFO_JS = r"""box => {
  const vis = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' &&
      s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
  };
  const isInfo = t => /টাইপ\s*[ঃ:]/.test(t) && /(লেভেল|টপিক)\s*[ঃ:]/.test(t);
  const all = [...document.body.querySelectorAll('*')].filter(e => {
    const t = e.textContent || '';
    return t.length < 500 && isInfo(t) && vis(e);
  });
  const leaf = all.filter(e => !all.some(o => o !== e && e.contains(o)));
  if (!leaf.length) return null;
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
  const dist = e => {
    const r = e.getBoundingClientRect();
    return Math.hypot(r.x + r.width / 2 - cx, r.y + r.height / 2 - cy);
  };
  leaf.sort((a, b) => dist(a) - dist(b));
  const t = (leaf[0].innerText || leaf[0].textContent || '').replace(/\u00a0/g, ' ');
  const grab = lab => {
    const m = t.match(new RegExp(lab + '\\s*[ঃ:]\\s*([\\s\\S]*?)(?=\\s*(?:লেভেল|টপিক|টাইপ)\\s*[ঃ:]|\\n|$)'));
    return m ? m[1].replace(/\s+/g, ' ').trim() : '';
  };
  return {type: grab('টাইপ'), level: grab('লেভেল'), topic: grab('টপিক')};
}"""


def read_filters(page) -> dict[str, str]:
    raw = page.evaluate(READ_FILTERS_JS)
    subject_raw = raw.get("subject", "")
    cls, sub = "", subject_raw
    m = re.split(r"\s+-\s+", subject_raw, maxsplit=1)
    if len(m) == 2:
        cls, sub = m[0].strip(), m[1].strip()
    if raw.get("class"):
        cls = raw["class"]
    topic = raw.get("topic") or "All Topics"
    chapter = raw.get("chapter", "")
    return {
        "class": cls, "subject": sub, "subject_raw": subject_raw, "chapter": chapter,
        "chapter_no": chapter_number(chapter), "topic": topic,
        "url": raw.get("url", ""),
    }


def chapter_number(text: str) -> str:
    t = N(text or "").translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
    m = re.search(
        N(r"(?:chapter|অধ্যায়|চ্যাপ্টার)") + r"\s*[-:#.]?\s*(\d+)", t, re.I
    ) or re.search(r"^\s*(\d+)\s*[.:)\-]", t)
    return m.group(1) if m else ""


def resolve_metadata(page, last: dict[str, str] | None) -> dict[str, str]:
    meta = read_filters(page)
    print("\nপেজ থেকে পাওয়া তথ্য:")
    for key in ("class", "subject", "chapter", "topic"):
        print(f"  {key.capitalize():8}: {meta[key] or '— পাওয়া যায়নি —'}")
    for key in ("class", "subject", "chapter"):
        if not meta[key]:
            hint = (last or {}).get(key, "")
            ans = input(f"{key.capitalize()} [{hint}] (Enter = এটাই): ").strip()
            meta[key] = ans or hint
    return meta


# --------------------------------------------------------------- scraping

MARK_CARDS_JS = r"""() => {
  document.querySelectorAll('[data-dc]').forEach(e => e.removeAttribute('data-dc'));
  const txt = e => (e.textContent || '').replace(/\s+/g, ' ').trim();
  const isBtn = e => e.children.length === 0 && /^Show\s+Answer\s*&\s*Solution$/i.test(txt(e));
  const all = () => [...document.querySelectorAll('span,button,div,a,p')];
  const btns = all().filter(isBtn);
  const cards = [];
  for (const b of btns) {
    let node = b;
    while (node.parentElement) {
      const p = node.parentElement;
      const n = [...p.querySelectorAll('span,button,div,a,p')].filter(isBtn).length;
      if (n !== 1 || txt(p).length > 30000) break;
      node = p;
    }
    if (!cards.includes(node)) cards.push(node);
  }
  cards.forEach((c, i) => c.setAttribute('data-dc', String(i)));
  return cards.length;
}"""


def find_question_cards(page):
    try:
        page.evaluate(MARK_CARDS_JS)
    except Exception:
        pass
    return page.locator("[data-dc]")


def find_question_page(context, preferred_page):
    pages = list(context.pages)
    if preferred_page in pages:
        pages.remove(preferred_page)
        pages.insert(0, preferred_page)
    for candidate in pages:
        try:
            if find_question_cards(candidate).count():
                return candidate
        except Exception:
            continue
    return None


def active_page_number(page) -> str:
    try:
        return page.locator(
            'button.mantine-Pagination-control[aria-current="page"]'
        ).first.inner_text(timeout=2000).strip()
    except Exception:
        return ""


def settle(page, tries: int = 10) -> None:
    prev = None
    for _ in range(tries):
        cards = find_question_cards(page)
        first = cards.first.inner_text(timeout=2000)[:120] if cards.count() else ""
        filters = read_filters(page)
        snap = (
            filters["topic"], filters["chapter"], first, active_page_number(page)
        )
        if snap == prev:
            return
        prev = snap
        page.wait_for_timeout(800)


def scroll_page_fully(page) -> None:
    page.evaluate(r"""async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      let last = -1, same = 0;
      for (let pass = 0; pass < 60 && same < 3; pass++) {
        window.scrollBy(0, Math.max(400, innerHeight * 0.8));
        await sleep(250);
        const h = document.body.scrollHeight;
        const atBottom = innerHeight + scrollY >= h - 4;
        if (atBottom && h === last) same++; else same = 0;
        last = h;
      }
      window.scrollTo(0, 0);
    }""")
    page.wait_for_timeout(300)


def _complete(p: dict, is_mcq: bool) -> bool:
    return bool(p.get("solution")) and (bool(p.get("answer")) or not is_mcq)


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k in ("question", "answer", "solution"):
        if not out.get(k) and b.get(k):
            out[k] = b[k]
    if not out.get("options") and b.get("options"):
        out["options"] = b["options"]
    seen = {(i["src"], i["where"]) for i in out.get("images", [])}
    out["images"] = list(out.get("images", [])) + [
        i for i in b.get("images", [])
        if (i["src"], i["where"]) not in seen
    ]
    return out


# ----------------------------------------------------------------- images

def real_image_url(u: str) -> str:
    try:
        p = urlparse(u)
        if p.path.endswith("/_next/image"):
            q = parse_qs(p.query).get("url")
            if q:
                return urljoin(u, unquote(q[0]))
    except Exception:
        pass
    return u


def img_key(u: str) -> str:
    if u.startswith("data:"):
        return "data:" + hashlib.sha1(u.encode()).hexdigest()
    try:
        return urlparse(u)._replace(query="", fragment="").geturl()
    except Exception:
        return u


def download_image(page, url: str) -> str:
    try:
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        if url.startswith("data:"):
            head, _, payload = url.partition(",")
            mime = head[5:].split(";")[0] or "image/png"
            body = base64.b64decode(payload) if ";base64" in head else unquote(payload).encode()
        else:
            resp = page.context.request.get(url, timeout=20000)
            if not resp.ok:
                print(f"  সতর্কতা: ছবি নামানো যায়নি ({resp.status}): {url[:80]}")
                return ""
            body = resp.body()
            mime = resp.headers.get("content-type", "")
        ext = (
            mimetypes.guess_extension(mime.split(";")[0].strip())
            or Path(urlparse(url).path).suffix
            or ".img"
        )
        if ext == ".jpe":
            ext = ".jpg"
        name = hashlib.sha1(body).hexdigest()[:16] + ext
        path = IMG_DIR / name
        if not path.exists():
            path.write_bytes(body)
        return f"{IMG_DIR.name}/{name}"
    except Exception as exc:
        print(f"  সতর্কতা: ছবি সেভ হয়নি: {exc}")
        return ""


def save_images(page, imgs: list[dict]) -> list[dict]:
    out, seen = [], set()
    want = bool(CFG.get("download_images", True))
    for im in imgs:
        url = real_image_url(im["src"])
        key = (img_key(url), im["where"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "in": im["where"],
            "url": "" if url.startswith("data:") else url,
            "file": download_image(page, url) if want else "",
        })
    return out


# ------------------------------------------------------------ question type

_INFO_MODE: dict[str, str | None] = {"mode": None}


def _close_popup(page) -> None:
    try:
        page.keyboard.press("Escape")
        page.mouse.move(2, 2)
        page.wait_for_timeout(120)
    except Exception:
        pass


def read_question_type(card) -> str:
    page = card.page
    try:
        n = card.evaluate(MARK_ICONS_JS)
    except Exception:
        return ""
    for i in range(n):
        btn = card.locator(f'[data-dc-ic="{i}"]').first
        modes = ["hover", "click"] if i == 0 else ["hover"]
        if i == 0 and _INFO_MODE["mode"]:
            modes = [_INFO_MODE["mode"]]
        for mode in modes:
            try:
                box = btn.bounding_box()
                if not box:
                    break
                if mode == "hover":
                    btn.hover(timeout=2500)
                else:
                    btn.click(timeout=2500, no_wait_after=True)
                info = None
                for _ in range(4 if mode == "hover" else 8):
                    page.wait_for_timeout(200)
                    info = page.evaluate(READ_INFO_JS, box)
                    if info and info.get("type"):
                        break
                    info = None
                _close_popup(page)
                if info:
                    if i == 0:
                        _INFO_MODE["mode"] = mode
                    return info["type"]
            except Exception:
                _close_popup(page)
    return ""


CQ_HINT = re.compile(r"[?।]|কর[।\s]|কর$|লেখ|বিশ্লেষণ|ব্যাখ্যা|মূল্যায়ন|বর্ণনা")
LONG_HINT = re.compile(N(r"বর্ণনা|আলোচনা|বিশ্লেষণ|ব্যাখ্যা|মূল্যায়ন|রচনা|প্রবন্ধ|বিস্তারিত"))
SUBQ_KA = re.compile(r"(^|\s)ক[.)]")
SUBQ_KHA = re.compile(r"(^|\s)খ[.)]")
BLANK_RE = re.compile(r"—|_{3,}|…|\.{4,}")


def classify_question(question: str, options: list[str], solution: str = "") -> str:
    """সাইটের (i) তথ্য না পাওয়া গেলে ফ্যালব্যাক: mcq / cq / fill / long / short।"""
    if options:
        hints = sum(1 for o in options if CQ_HINT.search(N(o)))
        return "cq" if (hints >= 2 and hints * 2 >= len(options)) else "mcq"
    if SUBQ_KA.search(question) and SUBQ_KHA.search(question):
        return "cq"
    if BLANK_RE.search(question):
        return "fill"
    if LONG_HINT.search(N(question)) or len(solution or "") > 400:
        return "long"
    return "short"


def make_id(meta, question: str, img_keys: list[str] | None = None) -> str:
    identity = "\n".join(
        [meta["class"], meta["subject"], meta["chapter"], meta["topic"], question]
        + sorted(img_keys or [])
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


# ---- কপি রোধ ও append-only নম্বর -------------------------------------------
# QINDEX: (শ্রেণি, বিষয়, অধ্যায়, প্রশ্ন)-এর ছাপ। Topic ধরা হয় না, তাই "All Topics" দিয়ে আগে
# নেওয়া প্রশ্ন Topic ধরে ধরে নেওয়ার সময় আবার জমা হয় না।
QINDEX: set[str] = set()
TOPIC_SEQ: dict[str, int] = {}


def qkey(meta, question: str, img_keys: list[str] | None = None) -> str:
    identity = "\n".join(
        [meta["class"], meta["subject"], meta["chapter"], "Q", question] + sorted(img_keys or [])
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def rebuild_index(records: dict[str, dict[str, Any]]) -> None:
    QINDEX.clear()
    TOPIC_SEQ.clear()
    for r in records.values():
        meta = {"class": r["class"], "subject": r["subject"],
                "chapter": r["chapter"], "topic": r["topic"]}
        imgs = [img_key(real_image_url(i.get("url", ""))) for i in r.get("images", [])
                if i.get("in") == "question" and i.get("url")]
        QINDEX.add(qkey(meta, r["question_text"], imgs))
        k = topic_key(meta)
        TOPIC_SEQ[k] = max(TOPIC_SEQ.get(k, 0), int(r.get("seq") or 0))


def next_seq(meta) -> int:
    """এই Topic-এর শেষ নম্বরের পরের নম্বর (আগে ১–২২ থাকলে নতুনটা ২৩)।"""
    k = topic_key(meta)
    TOPIC_SEQ[k] = TOPIC_SEQ.get(k, 0) + 1
    return TOPIC_SEQ[k]


def _q_and_imgs(payload: dict) -> tuple[str, list[str]]:
    q = (payload.get("question") or "").strip()
    qi = [img_key(real_image_url(i["src"])) for i in payload.get("images", [])
          if i["where"] == "question"]
    return q, qi


def _already_have(meta, question: str, q_imgs: list[str], records) -> bool:
    if not (question or q_imgs):
        return False
    return make_id(meta, question, q_imgs) in records or qkey(meta, question, q_imgs) in QINDEX


def extract_card(card, meta, page_no: str, records):
    page = card.page

    # ১) আগে সস্তা পাঠ: নেওয়া প্রশ্ন হলে (i)-আইকন/click ছাড়াই বাদ।
    payload = card.evaluate(EXTRACT_CARD_JS, "")
    question, q_imgs = _q_and_imgs(payload)
    if _already_have(meta, question, q_imgs, records):
        return {"_skip": True}

    # ২) নতুন প্রশ্ন: type পড়া, তারপর পূর্ণ extraction.
    qtype = read_question_type(card)
    payload = card.evaluate(EXTRACT_CARD_JS, qtype)
    question, q_imgs = _q_and_imgs(payload)
    if _already_have(meta, question, q_imgs, records):
        return {"_skip": True}
    rid = make_id(meta, question, q_imgs)
    qk = qkey(meta, question, q_imgs)

    is_mcq = bool(payload.get("options"))
    if not _complete(payload, is_mcq):
        btn = card.get_by_text(SHOW_BUTTON_TEXT)
        if btn.count():
            for _ in range(2):
                try:
                    btn.first.click(timeout=4000, no_wait_after=True)
                    page.wait_for_timeout(500)
                    payload = _merge(payload, card.evaluate(EXTRACT_CARD_JS, qtype))
                    if _complete(payload, is_mcq):
                        break
                except Exception as exc:
                    print(f"  সতর্কতা: Show Answer খোলা যায়নি: {exc}")
                    break

    if not qtype:  # (i) থেকে ধরন না পেলে লেখা দেখে অনুমান
        qtype = classify_question(
            (payload.get("question") or question).strip(),
            payload.get("options") or [],
            payload.get("solution", ""),
        )

    return {
        "id": rid,
        "_qk": qk,
        "class": meta["class"],
        "subject": meta["subject"],
        "chapter_no": meta["chapter_no"],
        "chapter": meta["chapter"],
        "topic": meta["topic"],
        "qtype": qtype,
        "question_text": (payload.get("question") or question).strip(),
        "options": payload.get("options") or [],
        "answer": payload.get("answer", ""),
        "solution": payload.get("solution", ""),
        "images": save_images(page, payload.get("images", [])),
    }


def next_button(page):
    btns = page.locator("button.mantine-Pagination-control")
    labels = []
    for i in range(btns.count()):
        b = btns.nth(i)
        labels.append((
            b, (b.inner_text(timeout=500) or "").strip(),
            b.get_attribute("aria-current") == "page",
        ))
    active = next((i for i, (_, _, cur) in enumerate(labels) if cur), None)
    if active is None:
        return None
    for b, label, _ in labels[active + 1:]:
        if label.isdigit():
            return b
    return None


def topic_key(meta) -> str:
    return "|".join([meta["class"], meta["subject"], meta["chapter"], meta["topic"]])


def _dump_card(card, name: str, note: str) -> None:
    dbg = OUT / name
    if not dbg.exists():
        try:
            dbg.write_text(card.evaluate("e => e.outerHTML"), encoding="utf-8")
            print(f"  ℹ {note}: {dbg.name}")
        except Exception:
            pass


def _ignore_done() -> bool:
    return bool(CFG.get("ignore_done_markers", False))


def scrape_page(page, meta, page_no: str, records) -> tuple[bool, int]:
    """ফেরত: (সব প্রশ্ন ঠিকভাবে হয়েছে কি না, পৃষ্ঠার প্রশ্নসংখ্যা)।"""
    cards = find_question_cards(page)
    total = cards.count()
    print(f"পৃষ্ঠা {page_no}: {total}টি প্রশ্ন")
    done_idx: set[int] = set()
    new = skipped = 0

    for attempt in range(3):
        todo = [i for i in range(total) if i not in done_idx]
        if not todo:
            break
        if attempt:
            print(f"  আবার চেষ্টা ({attempt}): বাকি প্রশ্ন নম্বর {[i + 1 for i in todo]}")
            scroll_page_fully(page)

        for i in todo:
            try:
                cards = find_question_cards(page)
                if i >= cards.count():
                    raise RuntimeError(f"কার্ড তালিকা ছোট হয়ে গেছে ({cards.count()}টি)")
                card = cards.nth(i)
                card.scroll_into_view_if_needed(timeout=5000)
                rec = extract_card(card, meta, page_no, records)
                if rec.get("_skip"):
                    skipped += 1
                    done_idx.add(i)
                    continue
                if not rec["question_text"] and not rec["images"]:
                    print(f"  প্রশ্ন {i + 1}: লেখা পাওয়া যায়নি")
                    _dump_card(card, "debug_empty_card.html", "নমুনা সেভ")
                    continue

                qk = rec.pop("_qk", None)
                rec["seq"] = next_seq(meta)          # append-only: শেষ নম্বরের পর থেকে
                records[rec["id"]] = rec
                if qk:
                    QINDEX.add(qk)
                save_record(rec)
                done_idx.add(i)
                new += 1

                if not rec["solution"]:
                    _dump_card(card, "debug_missing_solution_card.html", "Solution না পাওয়ার নমুনা")
                if not rec["qtype"]:
                    _dump_card(card, "debug_no_type_card.html", "(i) থেকে type না পাওয়ার নমুনা")
                if card.evaluate("e => !!e.querySelector('math, mjx-container, .katex, .MathJax, script[type*=\"math\"], [class*=\"math\"]')"):
                    _dump_card(card, "debug_math_card.html", "গাণিতিক প্রশ্নের HTML (এটা পাঠান)")
                print(
                    f"  ✓ {i + 1} [টাইপ: {rec['qtype'] or '—'}] "
                    f"অপশন={len(rec['options'])}, ছবি={len(rec['images'])}, "
                    f"Answer={'আছে' if rec['answer'] else 'নেই'}, "
                    f"Solution={'আছে' if rec['solution'] else 'নেই'}"
                )
                time.sleep(float(CFG.get("delay_seconds", 0.5)))
            except Exception as exc:
                print(f"  ✗ প্রশ্ন {i + 1}: {exc}")

    missing = [i + 1 for i in range(total) if i not in done_idx]
    print(f"  নতুন: {new}টি, আগে থেকেই সংরক্ষিত ছিল: {skipped}টি, মোট {total}টির মধ্যে")
    if missing:
        print(f"  ⚠ বাদ পড়েছে (পৃষ্ঠার ক্রম অনুযায়ী নম্বর): {missing}")
    return (not missing), total


def go_to_first_page(page) -> None:
    """Topic বদলালে তালিকা প্রথম পৃষ্ঠায় না থাকলে ১ নম্বরে ফিরি।"""
    if active_page_number(page) in ("", "1"):
        return
    btns = page.locator("button.mantine-Pagination-control")
    for i in range(btns.count()):
        if (btns.nth(i).inner_text() or "").strip() == "1":
            try:
                btns.nth(i).click(timeout=5000, no_wait_after=True)
                page.wait_for_timeout(800)
                settle(page)
            except Exception:
                pass
            return


def collect_topic(page, meta, records, done: set[tuple[str, str]]) -> tuple[bool, int]:
    """ফেরত: (সব পৃষ্ঠা সম্পূর্ণ কি না, মোট প্রশ্ন; আগেই-সম্পূর্ণ পৃষ্ঠা থাকলে -1)।"""
    key = topic_key(meta)
    visited: set[str] = set()
    complete, total, unknown = True, 0, False
    while True:
        page_no = active_page_number(page) or "1"
        if page_no in visited:
            print("একই পৃষ্ঠা পুনরায় এসেছে; এই Topic-এ থামানো হলো।")
            complete = False
            break
        visited.add(page_no)

        if (key, page_no) in done and not _ignore_done():
            print(f"পৃষ্ঠা {page_no}: আগেই সম্পূর্ণ সংরক্ষিত; বাদ দিয়ে এগোচ্ছি")
            unknown = True
        else:
            scroll_page_fully(page)
            ok, n = scrape_page(page, meta, page_no, records)
            total += n
            export_json(records)
            if ok:
                mark_page_done(key, page_no)
                done.add((key, page_no))
            else:
                complete = False
                print("এই পৃষ্ঠায় কিছু ত্রুটি ছিল; পরের বার এটি আবার চেষ্টা হবে।")

        nxt = next_button(page)
        if nxt is None:
            break
        try:
            nxt.scroll_into_view_if_needed(timeout=3000)
            nxt.click(timeout=7000, no_wait_after=True)
            page.wait_for_function(
                """old => {
                  const e=document.querySelector('button.mantine-Pagination-control[aria-current="page"]');
                  return e && e.innerText.trim() !== old;
                }""",
                arg=page_no,
                timeout=15000,
            )
            page.wait_for_timeout(700)
            settle(page)
        except Exception as exc:
            print(f"পরের পৃষ্ঠা খোলা যায়নি; ভুল ডেটা এড়াতে থামানো হলো: {exc}")
            complete = False
            break
    return complete, (-1 if unknown else total)


# ================================================= স্বয়ংক্রিয় loop (শ্রেণি → বিষয় → অধ্যায় → Topic)

def init_units() -> None:
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS units_done (
            kind TEXT, key TEXT, PRIMARY KEY (kind, key))""")


def load_units() -> set[tuple[str, str]]:
    with db() as con:
        return set(con.execute("SELECT kind, key FROM units_done").fetchall())


def mark_unit(kind: str, key: str, units: set[tuple[str, str]]) -> None:
    with db() as con:
        con.execute("INSERT OR IGNORE INTO units_done VALUES (?,?)", (kind, key))
    units.add((kind, key))


FIND_INPUT_ID_JS = r"""(want) => {
  const N = s => s.normalize('NFD').replace(/[*:]/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
  const target = N(want);
  for (const lab of document.querySelectorAll('label')) {
    if (N(lab.textContent) === target && lab.htmlFor) return lab.htmlFor;
  }
  return '';
}"""

# Topic ঘরের নিজের chip-এর ✕ (selector config.json-এ chip_close_selector দিয়ে বদলানো যায়)।
CHIP_CLOSE_DEFAULT = ".sc-hrXxpv.eaMJSE .close-icon"

# ফ্যালব্যাক: selector-এর hash বদলে গেলে Topic ঘরের কাছের ✕ আইকন নিজে খুঁজে নেয়।
MARK_CLOSERS_JS = r"""(id) => {
  document.querySelectorAll('[data-dc-x]').forEach(e => e.removeAttribute('data-dc-x'));
  let node = document.getElementById(id);
  if (!node) return 0;
  for (let i = 0; i < 6 && node.parentElement; i++) {
    node = node.parentElement;
    const others = [...node.querySelectorAll('input')]
      .filter(x => x.type !== 'checkbox' && x.type !== 'hidden');
    if (others.length > 1) break;
    const c = [...node.querySelectorAll('.close-icon, [class*="close-icon"], [class*="closeIcon"]')];
    if (c.length) { c.forEach((e, k) => e.setAttribute('data-dc-x', String(k))); return c.length; }
  }
  return 0;
}"""


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", N(text)).strip().lower()


def _loose(text: str) -> str:
    return re.sub(r"[\s\-–—_.:/]+", "", norm(text))


def match_index(items: list[str], wanted: str) -> int | None:
    """আগে হুবহু মেলানো, না পেলে স্পেস/চিহ্ন বাদ দিয়ে মেলানো।"""
    for i, x in enumerate(items):
        if norm(x) == norm(wanted):
            return i
    for i, x in enumerate(items):
        if _loose(x) == _loose(wanted):
            return i
    return None


def is_all_option(text: str) -> bool:
    t = norm(text)
    return t.startswith(("all ", "সব ", "সকল ")) or t in ("all", "সব", "সকল")


def split_subject(opt: str) -> tuple[str, str]:
    m = re.split(r"\s+-\s+", opt or "", maxsplit=1)
    return (m[0].strip(), m[1].strip()) if len(m) == 2 else ("", (opt or "").strip())


def _filter_input(page, label: str):
    iid = page.evaluate(FIND_INPUT_ID_JS, label)
    return page.locator(f'[id="{iid}"]') if iid else None


def close_dropdown(page) -> None:
    """focus সরিয়ে dropdown বন্ধ। Escape নয়: search ঘরে Escape মান মুছে ফেলে।"""
    try:
        page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
        page.wait_for_timeout(250)
    except Exception:
        pass


def _read_options(page) -> list[str]:
    opts = page.locator('[role="option"]')
    try:
        opts.first.wait_for(state="visible", timeout=4000)
        return [t.strip() for t in opts.all_inner_texts() if t.strip()]
    except Exception:
        return []


def list_options(page, label: str) -> list[str]:
    """বিষয়/অধ্যায়/টপিকের dropdown খুলে সব option-এর লেখা নেয়।"""
    inp = _filter_input(page, label)
    if inp is None:
        return []
    inp.click()
    page.wait_for_timeout(300)
    texts = _read_options(page)
    if len(texts) <= 1 and label != "টপিক":
        # মান বসানো থাকলে তালিকা ছেঁকে যেতে পারে: search লেখা মুছে আবার পড়ি।
        try:
            inp.press("Control+a")
            inp.press("Backspace")
            page.wait_for_timeout(300)
            more = _read_options(page)
            if len(more) > len(texts):
                texts = more
        except Exception:
            pass
    if not texts:
        dbg = OUT / "debug_dropdown.html"
        if not dbg.exists():
            dbg.write_text(page.content(), encoding="utf-8")
            print(f"  ⚠ '{label}' এর তালিকা খোলেনি; নমুনা সেভ: {dbg.name}")
    close_dropdown(page)
    return texts


def choose_option(page, label: str, wanted: str) -> bool:
    inp = _filter_input(page, label)
    if inp is None:
        return False
    inp.click()
    page.wait_for_timeout(300)
    opts = page.locator('[role="option"]')
    try:
        opts.first.wait_for(state="visible", timeout=4000)
        texts = opts.all_inner_texts()
    except Exception:
        close_dropdown(page)
        return False
    idx = match_index([t.strip() for t in texts], wanted)
    if idx is None:
        close_dropdown(page)
        return False
    opts.nth(idx).click(no_wait_after=True)
    page.wait_for_timeout(350)
    return True


# ---- Topic chip: একটি Topic বাছলে অন্যটা বাছার আগে আগেরটা কেটে দিতে হয় ----------

def _closers(page):
    loc = page.locator(CFG.get("chip_close_selector", CHIP_CLOSE_DEFAULT))
    if loc.count():
        return loc
    try:
        iid = page.evaluate(FIND_INPUT_ID_JS, "টপিক")
        if iid and page.evaluate(MARK_CLOSERS_JS, iid):
            return page.locator("[data-dc-x]")
    except Exception:
        pass
    return None


def chips_count(page) -> int:
    loc = _closers(page)
    return loc.count() if loc is not None else 0


def clear_topic_chips(page) -> int:
    """নির্বাচিত সব Topic chip-এর ✕ চেপে কাটে; তারপর পেজ আপডেটের জন্য অপেক্ষা।"""
    removed = 0
    for _ in range(30):
        loc = _closers(page)
        if loc is None or loc.count() == 0:
            break
        try:
            loc.first.click(timeout=3000, no_wait_after=True)
        except Exception:
            break
        removed += 1
        page.wait_for_timeout(500)
    if removed:
        settle(page)
    return removed


_CHIP_WARNED = {"done": False}


def select_topic(page, topic: str) -> bool:
    """পুরোনো chip কেটে ফেলে শুধু এই একটি Topic বাছে। সফল হলে True।"""
    clear_topic_chips(page)
    for attempt in range(2):
        if not choose_option(page, "টপিক", topic):
            return False
        close_dropdown(page)
        page.wait_for_timeout(600)
        n = chips_count(page)
        if n == 0 and not _CHIP_WARNED["done"]:
            _CHIP_WARNED["done"] = True
            print("  ⚠⚠ Topic বাছার পরেও chip-এর ✕ খুঁজে পাওয়া যাচ্ছে না। আগের Topic কাটা যাচ্ছে না,"
                  " তাই একাধিক Topic টিক থাকতে পারে।")
            print("     config.json-এ \"chip_close_selector\" দিন (যেমন \".sc-hrXxpv.eaMJSE .close-icon\")।"
                  " প্রশ্ন কপি হবে না, কিন্তু Topic-এর সীমানা ঠিক থাকবে না।")
        if n <= 1:
            settle(page)
            return True
        print("  ⚠ একাধিক Topic টিক ছিল; chip কেটে আবার বাছছি")
        clear_topic_chips(page)
    return False


# ---- loop --------------------------------------------------------------------

def print_topic_header(meta: dict, where: str) -> None:
    print("\n" + "═" * 66)
    print(f" Class: {meta['class'] or '—'}   |   বিষয়: {meta['subject'] or '—'}")
    print(f" অধ্যায়: {meta['chapter'] or '—'}")
    print(f" Topic: {meta['topic']}      [{where}]")
    print("═" * 66)


def run_class(page, records, done, units) -> str:
    """বর্তমান শ্রেণির সব বিষয়: প্রতি বিষয়ের সব অধ্যায়, প্রতি অধ্যায়ের সব Topic একটার পর একটা।
    সব শেষ হলে থামে। "All Topics" বাদ। খালি অধ্যায়/Topic কেবল বাদ দিয়ে এগিয়ে যায়।"""
    now = read_filters(page)
    subjects_all = list_options(page, "বিষয়")
    if not subjects_all:
        print("⚠ বিষয়ের তালিকা পড়া যায়নি; শুধু বর্তমান Topic নেওয়া হচ্ছে।")
        meta = read_filters(page)
        collect_topic(page, meta, records, done)
        return "fallback"

    cls = norm(now["class"])
    subjects = [s for s in subjects_all if not cls or norm(split_subject(s)[0]) == cls] or subjects_all
    start = 0
    if CFG.get("start_from_current", False):
        start = match_index(subjects, now["subject_raw"]) or 0
    print(f"\nশ্রেণি: {now['class'] or '—'} — মোট {len(subjects)}টি বিষয়"
          + (f" (বর্তমান বিষয় থেকে শুরু)" if start else ""))

    ignore = _ignore_done()
    for s_idx in range(start, len(subjects)):
        subj = subjects[s_idx]
        s_cls, s_name = split_subject(subj)
        s_key = f"{s_cls}|{s_name}"
        tag_s = f"বিষয় {s_idx + 1}/{len(subjects)}"
        if ("subject", s_key) in units and not ignore:
            print(f"\n✔ {tag_s} '{s_name}' আগেই সম্পূর্ণ; বাদ।")
            continue

        if norm(read_filters(page)["subject_raw"]) != norm(subj):
            if not choose_option(page, "বিষয়", subj):
                print(f"✗ বিষয় বেছে নেওয়া যায়নি: {subj}; এগিয়ে যাচ্ছি।")
                continue
            settle(page)

        clear_topic_chips(page)
        chapters = [c for c in list_options(page, "অধ্যায়") if not is_all_option(c)]
        if not chapters:
            print(f"\nℹ {tag_s} '{s_name}': কোনো অধ্যায় নেই; এগিয়ে যাচ্ছি।")
            continue

        subject_ok = True
        for c_idx, chap in enumerate(chapters):
            tag_c = f"{tag_s} · অধ্যায় {c_idx + 1}/{len(chapters)}"
            c_key = f"{s_key}|{chap}"
            if ("chapter", c_key) in units and not ignore:
                print(f"✔ {tag_c} '{chap}' আগেই সম্পূর্ণ; বাদ।")
                continue

            if norm(read_filters(page)["chapter"]) != norm(chap):
                if not choose_option(page, "অধ্যায়", chap):
                    print(f"✗ অধ্যায় বেছে নেওয়া যায়নি: {chap}; এগিয়ে যাচ্ছি।")
                    subject_ok = False
                    continue
                settle(page)

            clear_topic_chips(page)  # তালিকায় আগে বাছা Topic লুকিয়ে যায়; তাই আগে পরিষ্কার
            topics = [t for t in list_options(page, "টপিক") if not is_all_option(t)]
            if not topics:
                n = find_question_cards(page).count()
                if n and CFG.get("collect_when_no_topics", False):
                    meta = read_filters(page)
                    meta["topic"] = "All Topics"
                    print_topic_header(meta, tag_c + " · Topic নেই")
                    complete, _ = collect_topic(page, meta, records, done)
                    if complete:
                        mark_unit("chapter", c_key, units)
                    else:
                        subject_ok = False
                else:
                    print(f"ℹ {tag_c} '{chap}': আলাদা Topic নেই"
                          + (f" ({n}টি প্রশ্ন দেখা যাচ্ছে, নিয়ম অনুযায়ী বাদ)" if n else " (খালি)")
                          + "; এগিয়ে যাচ্ছি।")
                continue

            chapter_ok = True
            for t_idx, topic in enumerate(topics):
                tag_t = f"{tag_c} · Topic {t_idx + 1}/{len(topics)}"
                meta = read_filters(page)
                meta["topic"] = topic
                meta["chapter_no"] = chapter_number(meta["chapter"])
                t_key = topic_key(meta)
                if ("topic", t_key) in units and not ignore:
                    print(f"✔ {tag_t} '{topic}' আগেই সম্পূর্ণ; বাদ।")
                    continue
                try:
                    if not select_topic(page, topic):
                        print(f"✗ {tag_t}: Topic '{topic}' বাছা যায়নি; এগিয়ে যাচ্ছি।")
                        chapter_ok = False
                        continue
                    go_to_first_page(page)
                    print_topic_header(meta, tag_t)
                    complete, total = collect_topic(page, meta, records, done)
                    if total == 0:
                        print("  ℹ খালি Topic — কিছু নেই; এগিয়ে যাচ্ছি।")
                    if complete:
                        mark_unit("topic", t_key, units)
                    else:
                        chapter_ok = False
                    export_json(records)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"✗ {tag_t}: ত্রুটি: {exc}; এগিয়ে যাচ্ছি।")
                    chapter_ok = False
                finally:
                    try:
                        clear_topic_chips(page)  # শেষ হলে chip কেটে দেওয়া, পরেরটার জন্য
                    except Exception:
                        pass

            if chapter_ok:
                mark_unit("chapter", c_key, units)
            else:
                subject_ok = False

        if subject_ok:
            mark_unit("subject", s_key, units)

    print("\n✅ এই শ্রেণির সব বিষয় শেষ।")
    return "done"


# --------------------------------------------------------------------- main

def open_link_if_given(page, text: str) -> None:
    text = text.strip()
    if text.lower().startswith("http"):
        print(f"লিংক খুলছি: {text}")
        page.goto(text, wait_until="domcontentloaded")
        for _ in range(20):
            if find_question_cards(page).count():
                break
            page.wait_for_timeout(1000)


def main():
    from playwright.sync_api import sync_playwright

    init_db()
    init_progress()
    init_units()
    records = load_existing()
    done = load_progress()
    units = load_units()
    rebuild_index(records)
    print(f"আগে থেকে সংরক্ষিত প্রশ্ন: {len(records)}; সম্পূর্ণ পৃষ্ঠা: {len(done)}; "
          f"সম্পূর্ণ Topic/অধ্যায়/বিষয়: {len(units)}")
    print("=" * 66)
    print("Daricomma Collector — স্বয়ংক্রিয় (শ্রেণির সব বিষয়)")
    print("=" * 66)
    print("১) Browser-এ Login করুন এবং প্রশ্নের পেজ খুলুন।")
    print("২) যে শ্রেণির জন্য চালাতে চান, সাইটে সেই শ্রেণির একটি বিষয় বেছে রাখুন।")
    print("৩) ফিরে এসে Enter দিন; বাকি সব প্রোগ্রাম নিজে করবে (Topic → অধ্যায় → বিষয়)।")
    print("থামাতে Ctrl+C; যা নেওয়া হয়েছে সব সেভ থাকে, পরের বার সেখান থেকে চলবে।")
    print("পাসওয়ার্ড/OTP এখানে দেবেন না; browser_profile কাউকে দেবেন না।")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        if not page.url or page.url == "about:blank":
            page.goto(
                CFG.get("start_url", "https://daricomma.com/academic/subject"),
                wait_until="domcontentloaded",
            )

        link = input("প্রশ্নের পেজ প্রস্তুত হলে Enter (অথবা লিংক paste করুন): ")
        open_link_if_given(page, link)
        active = find_question_page(context, page)
        if active is None:
            print("প্রশ্ন শনাক্ত হয়নি। সঠিক পেজে গিয়ে আবার চালান।")
            context.close()
            return

        page = active
        try:
            while True:
                settle(page)
                print(f"\nবর্তমান পেজ: {page.url}")
                if CFG.get("auto_loop", True):
                    run_class(page, records, done, units)
                else:  # পুরোনো হাতে-চালানো নিয়ম: শুধু বর্তমান Topic
                    meta = resolve_metadata(page, None)
                    collect_topic(page, meta, records, done)
                export_json(records)
                print(f"এখন পর্যন্ত মোট অনন্য প্রশ্ন: {len(records)}")
                choice = input(
                    "\nশেষ। অন্য শ্রেণিতে যেতে সাইটে সেই শ্রেণির একটি বিষয় বেছে Enter দিন "
                    "(বা লিংক paste করুন)। থামতে 'q': "
                ).strip()
                if choice.lower() == "q":
                    break
                open_link_if_given(page, choice)
                active = find_question_page(context, page)
                if active is None:
                    print("নতুন পেজ শনাক্ত হয়নি; আগের পেজেই আছি।")
                    continue
                page = active
        except KeyboardInterrupt:
            print("\nCtrl+C: থামানো হলো। এ পর্যন্ত নেওয়া সব সেভ আছে; পরের বার সেখান থেকেই চলবে।")

        export_json(records)
        print(f"\nসংগ্রহ শেষ। মোট অনন্য প্রশ্ন: {len(records)}")
        print(f"JSON: {JSON_PATH}\nSQLite: {DB_PATH}")
        context.close()


if __name__ == "__main__":
    main()