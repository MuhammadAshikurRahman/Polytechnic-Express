"""Daricomma question collector update.

Opens a persistent local browser profile. The user logs in and manually navigates
through the site UI to the permitted question-list page. The collector then
clicks each question's Show Answer & Solution control and follows the enabled
Next pagination control until no next page remains.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
PROFILE = ROOT / "browser_profile"  # Never share this folder; it contains login state.
OUT = ROOT / CFG.get("output_dir", "output")
OUT.mkdir(parents=True, exist_ok=True)
DB_PATH = ROOT / CFG.get("database", "output/daricomma_questions.sqlite3")
JSON_PATH = ROOT / CFG.get("json_export", "output/daricomma_questions.json")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# The inspected HTML used this styled-components class. Keep a broad fallback
# because production deployments can regenerate the suffix class names.
CARD_SELECTORS = [".sc-fXpByb.iPsKXx", 'div[class*="sc-fXpByb"]']


def find_question_cards(target_page):
    """Return a locator for question cards, preferring the exact inspected class."""
    for selector in CARD_SELECTORS:
        loc = target_page.locator(selector)
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            pass
    # Last-resort structural match: a card-like container must contain both
    # a question text node and the site's Show Answer & Solution control.
    try:
        indexes = target_page.locator('div[class*="sc-fXpByb"]').evaluate_all("""els => {
          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
          return els.map((el, i) => ({i, el})).filter(({el}) => {
            const q = el.querySelector('[class*="sc-ceWscU"]');
            const txt = norm(el.innerText);
            const parentCard = el.parentElement && el.parentElement.closest('div[class*="sc-fXpByb"]');
            return q && /Show Answer\s*&\s*Solution/i.test(txt) && !parentCard;
          }).map(x => x.i);
        }""")
        if indexes:
            return target_page.locator('div[class*="sc-fXpByb"]').filter(has=target_page.get_by_text(ANSWER_BUTTON_TEXT)).filter(has=target_page.locator('[class*="sc-ceWscU"]'))
    except Exception:
        pass
    return target_page.locator('__no_such_question_card__')


def find_question_page(context, preferred_page):
    """Find the tab actually showing the question list; users may open a new tab."""
    candidates = list(context.pages)
    if preferred_page in candidates:
        candidates.remove(preferred_page)
        candidates.insert(0, preferred_page)
    for candidate in reversed(candidates):
        try:
            if any(candidate.locator(sel).count() for sel in CARD_SELECTORS):
                return candidate
            broad = candidate.locator('div[class*="sc-fXpByb"]')
            if broad.count() and candidate.get_by_text(ANSWER_BUTTON_TEXT).count():
                return candidate
        except Exception:
            continue
    return None

ANSWER_BUTTON_TEXT = re.compile(r"Show\s+Answer\s*&\s*Solution", re.I)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY,
            page_url TEXT NOT NULL,
            page_number TEXT,
            position INTEGER,
            question_text TEXT,
            options_json TEXT,
            answer_text TEXT,
            solution_text TEXT,
            card_html TEXT,
            collected_at TEXT NOT NULL
        )""")


def load_existing() -> dict[str, dict[str, Any]]:
    if not JSON_PATH.exists():
        return {}
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        return {x["id"]: x for x in data if x.get("id")}
    except Exception:
        return {}


def save_record(record: dict[str, Any]) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""INSERT OR REPLACE INTO questions
            (id,page_url,page_number,position,question_text,options_json,answer_text,solution_text,card_html,collected_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            record["id"], record["page_url"], record["page_number"], record["position"],
            record["question_text"], json.dumps(record["options"], ensure_ascii=False),
            record["answer"], record["solution"], record["card_html"], record["collected_at"]
        ))


def export_json(records: dict[str, dict[str, Any]]) -> None:
    JSON_PATH.write_text(json.dumps(list(records.values()), ensure_ascii=False, indent=2), encoding="utf-8")


def active_page_number(page) -> str:
    active = page.locator('button.mantine-Pagination-control[aria-current="page"]')
    try:
        return active.first.inner_text(timeout=2500).strip()
    except Exception:
        return ""


def extract_card(card, page_url: str, page_no: str, position: int) -> dict[str, Any]:
    # Reveal the actual answer and solution using the site's own UI control.
    button = card.get_by_text(ANSWER_BUTTON_TEXT)
    if button.count() > 0:
        try:
            button.first.click(timeout=5000)
            panel = card.locator(".mantine-1avyp1d").first
            try:
                panel.wait_for(state="visible", timeout=5000)
            except PlaywrightTimeoutError:
                # Some Mantine transitions leave aria-hidden briefly; allow the DOM update.
                page_wait = card.page if hasattr(card, "page") else None
                if page_wait:
                    page_wait.wait_for_timeout(500)
        except Exception as exc:
            print(f"  সতর্কতা: Answer button click হয়নি (প্রশ্ন {position}): {exc}")

    payload = card.evaluate("""(root) => {
      const txt = (el) => el ? (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim() : '';
      const q = root.querySelector('.sc-ceWscU.yigqC');
      const optionEls = [...root.querySelectorAll('.sc-ityoPf.fONsQU .sc-jxNRli.fhRjBD')];
      const options = optionEls.map(txt).filter(Boolean);
      const panels = [...root.querySelectorAll('.mantine-1avyp1d')];
      let answer = '';
      let solution = '';
      for (const panel of panels) {
        const labels = [...panel.querySelectorAll('.sc-goYBXY.bZxveq')];
        for (const label of labels) {
          const t = txt(label);
          if (/^Answer\\s*:/i.test(t)) answer = t.replace(/^Answer\\s*:\\s*/i,'').trim();
        }
        const solLabel = labels.find(el => /^Solution\\s*:/i.test(txt(el)));
        if (solLabel) {
          const solutionBox = panel.querySelector('.sc-dogGTU.ceXubr');
          solution = txt(solutionBox);
        }
      }
      return {question: txt(q), options, answer, solution, html: root.outerHTML};
    }""")
    question = payload.get("question", "").strip()
    stable = hashlib.sha256((page_url + "\n" + question).encode("utf-8")).hexdigest()
    return {
        "id": stable,
        "page_url": page_url,
        "page_number": page_no,
        "position": position,
        "question_text": question,
        "options": payload.get("options", []),
        "answer": payload.get("answer", ""),
        "solution": payload.get("solution", ""),
        "card_html": payload.get("html", ""),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def next_button(page):
    active = page.locator('button.mantine-Pagination-control[aria-current="page"]')
    if active.count() == 0:
        return None
    group = active.first.locator("xpath=..").locator("xpath=..")
    controls = group.locator("button.mantine-Pagination-control")
    if controls.count() < 3:
        return None
    candidate = controls.last
    try:
        if candidate.is_disabled() or candidate.get_attribute("data-disabled") is not None:
            return None
        # Mantine's right-chevron pagination control is the final button in this group.
        if candidate.locator("svg path").count() == 0:
            return None
        return candidate
    except Exception:
        return None


def main() -> None:
    init_db()
    records = load_existing()
    print("=" * 66)
    print("Daricomma Collector — Question + Answer + Solution")
    print("=" * 66)
    print("১) ব্রাউজার খুললে নিজের অ্যাকাউন্টে Login করো।")
    print("২) ওয়েবসাইটের স্বাভাবিক UI ব্যবহার করে নির্দিষ্ট Subject/Chapter-এর প্রশ্নের পেজে যাও।")
    print("৩) প্রশ্নের তালিকা দেখা গেলে CMD-তে Enter চাপো।")
    print("পাসওয়ার্ড/OTP এই প্রোগ্রামে লিখবে না।")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=False,
            viewport={"width": 1440, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        if not page.url or page.url == "about:blank":
            page.goto(CFG.get("start_url", "https://daricomma.com/academic/subject"), wait_until="domcontentloaded")
        input("প্রশ্নের পেজ প্রস্তুত হলে এখানে Enter চাপো... ")

        # Re-check every open tab after the user navigates; some site links open
        # a second tab while the initial tab remains on the landing page.
        active = find_question_page(context, page)
        if active is None:
            print("খোলা সব Tab-এ Question Card পাওয়া যায়নি। ৬০ সেকেন্ড অপেক্ষা করছি...")
            for remaining in range(60, 0, -5):
                context.pages[-1].wait_for_timeout(5000)
                active = find_question_page(context, page)
                if active is not None:
                    break
                print(f"আরও অপেক্ষা: {remaining-5} সেকেন্ড")
        if active is None:
            print("\nQuestion Page শনাক্ত হয়নি। নিচে খোলা Tab-গুলোর ঠিকানা দেওয়া হলো:")
            for i, tab in enumerate(context.pages, 1):
                print(f"  Tab {i}: {tab.url}")
            try:
                debug_page = context.pages[-1]
                (ROOT / "debug_page.html").write_text(debug_page.content(), encoding="utf-8")
                debug_page.screenshot(path=str(ROOT / "debug_page.png"), full_page=True)
                print("debug_page.html ও debug_page.png তৈরি হয়েছে। এগুলোতে Login তথ্য থাকলে শেয়ার করার আগে মুছে দেবে।")
            except Exception as exc:
                print(f"Diagnostic সংরক্ষণ করা যায়নি: {exc}")
            print("\nসঠিক Question List পেজে যাও, তারপর CMD-তে Enter চাপো।")
            input("পেজ ঠিক করার পর Enter চাপো; বন্ধ করতে Ctrl+C চাপো... ")
            active = find_question_page(context, page)
            if active is None:
                print("এখনো Question Card পাওয়া যায়নি। Browser খোলা রাখা হলো।")
                input("Browser নিজে বন্ধ করে CMD-তে Enter চাপো... ")
                context.close()
                return
        page = active
        print(f"Question Page শনাক্ত হয়েছে: {page.url}")

        visited_pages: set[str] = set()
        page_count = 0
        while True:
            page_no = active_page_number(page) or str(page_count + 1)
            current_url = page.url
            page_key = current_url + "#page=" + page_no
            if page_key in visited_pages:
                print("একই পৃষ্ঠা আবার এসেছে; নিরাপত্তার জন্য থামানো হলো।")
                break
            visited_pages.add(page_key)
            page_count += 1
            cards = find_question_cards(page)
            count = cards.count()
            print(f"\nপৃষ্ঠা {page_no}: {count}টি প্রশ্ন পাওয়া গেছে।")

            for i in range(count):
                card = cards.nth(i)
                try:
                    record = extract_card(card, current_url, page_no, i + 1)
                    if not record["question_text"]:
                        print(f"  প্রশ্ন {i+1}: লেখা পাওয়া যায়নি; বাদ দেওয়া হলো।")
                        continue
                    records[record["id"]] = record
                    save_record(record)
                    export_json(records)
                    print(f"  ✓ প্রশ্ন {i+1}: Answer={'হ্যাঁ' if record['answer'] else 'নেই'}, Solution={'হ্যাঁ' if record['solution'] else 'নেই'}")
                except Exception as exc:
                    print(f"  ✗ প্রশ্ন {i+1} সংগ্রহে সমস্যা: {exc}")
                time.sleep(float(CFG.get("delay_seconds", 0.8)))

            nxt = next_button(page)
            if nxt is None:
                print("আর কোনো সক্রিয় Next Page নেই। সংগ্রহ শেষ।")
                break
            old_no = page_no
            try:
                nxt.click(timeout=5000)
                page.locator('button.mantine-Pagination-control[aria-current="page"]').wait_for(timeout=10000)
                page.wait_for_function(
                    "old => { const e=document.querySelector('button.mantine-Pagination-control[aria-current=page]'); return e && e.innerText.trim() !== old; }",
                    arg=old_no, timeout=10000,
                )
                page.wait_for_timeout(1000)
            except Exception as exc:
                print(f"Next পৃষ্ঠা খোলার সময় সমস্যা: {exc}")
                print("স্বয়ংক্রিয়ভাবে আর এগোনো হয়নি; ভুল পৃষ্ঠা বাদ না দিতে থামানো হলো।")
                break

        export_json(records)
        print("\n" + "=" * 66)
        print(f"সম্পন্ন। মোট অনন্য প্রশ্ন: {len(records)}")
        print(f"JSON: {JSON_PATH}")
        print(f"SQLite: {DB_PATH}")
        print("browser_profile ফোল্ডারটি কাউকে পাঠাবে না।")
        print("=" * 66)
        context.close()


if __name__ == "__main__":
    main()
