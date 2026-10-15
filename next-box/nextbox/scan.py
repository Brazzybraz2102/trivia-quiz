"""Phase 2 read-back: photo of a marked-up label -> to-do app changes, plus the journal page.

Auto-applied (confidence >= 0.7): ✓ done -> close, → tomorrow -> due_date = tomorrow.
Always held for confirmation: every ✗ drop (delete) and anything below 0.7.
When the photo also shows handwritten notes (the facing journal page), they're transcribed, and the
photo is kept as that day's journal page (nextbox/journal.py).
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Literal
from collections.abc import Callable

from pydantic import BaseModel, Field

from .jobs import Context

CONFIDENCE_THRESHOLD = 0.7
Mark = Literal["done", "tomorrow", "drop", "none"]


class RowMark(BaseModel):
    row: int = Field(description="Task row number, counting from 1 at the top")
    mark: Mark
    confidence: float = Field(description="0.0-1.0: how sure you are about this row's mark")
    note: str = Field(default="", description="Anything odd, e.g. 'mark is between rows 3 and 4'")


class ReadBack(BaseModel):
    label_code: str | None = Field(description="The #code printed bottom-right, without '#', or null if unreadable")
    rows: list[RowMark]
    notes: str = Field(default="", description="Handwritten notes outside the label, transcribed; '' if none")


# (image bytes, media type, manifest) -> ReadBack. Swapped for a fake in tests.
VisionFn = Callable[[bytes, str, list[dict]], ReadBack]

PROMPT = """This is a photo of a printed to-do label: a daily list, tear-off strips, a "just one thing"
card, or one or more single-task stickers. Each task has a checkbox. When a small row number is
printed next to or under a checkbox, use that number. Otherwise number the task rows 1, 2, 3...
from top to bottom (skip the "SCHEDULE" section at the top, which has no boxes, anything in the
"Also waiting" or "Not today" footer, and the small "tiny steps" boxes on a "just one thing" card).
The person marked rows by hand:
- a check mark (✓) in or near the box means "done"
- an arrow (→) means "move to tomorrow"
- a cross (✗ or X) or a line struck through the task means "drop"
- an empty box means "none"
- anything else (an up arrow, a circle, a note) is not one of these: use the closest mark with
  confidence below 0.5 and describe what you saw in the note

Report every numbered row you can see, with the mark and your confidence.
Be conservative: if a mark is ambiguous, smudged, or could belong to a neighbouring row, lower the confidence.
Also read the code printed at the bottom left after '#' (like 260930-X5C8).

The label may be stuck on the left page of a paper journal with handwritten notes on the right page
(or around the label). If there are handwritten notes that are not part of the printed label,
transcribe them into "notes" as faithfully as you can: keep the person's own words, line breaks and
lists, write [unclear] for a word you can't read, and don't add anything. If there are none, leave
"notes" empty. If the photo has notes but no label at all, report no rows and a null code.
{manifest_hint}"""


MAX_EDGE = 2048          # plenty for handwriting; keeps uploads small and cost down
MAX_PIXELS = 50_000_000  # refuse decompression bombs


class PhotoError(ValueError):
    pass


def prepare_photo(data: bytes) -> tuple[bytes, str]:
    """Phone photo -> upright JPEG no bigger than MAX_EDGE on its long side."""
    import io

    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))  # reads the header only; pixels aren't decoded yet
        if img.width * img.height > MAX_PIXELS:
            raise PhotoError("photo is far too large")
        img = ImageOps.exif_transpose(img)  # phones store rotation separately
        img = img.convert("RGB")
    except PhotoError:
        raise
    except Image.DecompressionBombError as exc:
        raise PhotoError("photo is far too large") from exc
    except Exception as exc:
        raise PhotoError("that file isn't a photo Next Box can read (try JPEG or PNG)") from exc
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue(), "image/jpeg"


def claude_vision(settings) -> VisionFn:
    import anthropic

    # A stuck request mustn't tie up the server: fail after 90 s instead of the SDK's 10 minutes.
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None, timeout=90.0, max_retries=2)

    def run(image: bytes, media_type: str, manifest: list[dict]) -> ReadBack:
        hint = ""
        if manifest:
            listing = "\n".join(f"{m['row']}. {m['content']}" for m in manifest)
            hint = f"\nThe label should list these task rows, in this order:\n{listing}"
        response = client.messages.parse(
            model=settings.vision_model,
            max_tokens=4000,
            output_config={"effort": "medium"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                 "data": base64.standard_b64encode(image).decode()}},
                    {"type": "text", "text": PROMPT.format(manifest_hint=hint)},
                ],
            }],
            output_format=ReadBack,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError(f"vision model returned no read-back (stop_reason={response.stop_reason})")
        return response.parsed_output

    return run


def _find_label(ctx: Context, code: str | None, label_id: str | None) -> dict | None:
    for candidate in (label_id, code):
        if candidate:
            rec = ctx.store.get_printed(candidate)
            # Only your own tickets: their task ids belong to your to-do app.
            if rec and rec["manifest"] and rec.get("by", ctx.user) == ctx.user:
                return rec
    return None


def _apply(ctx: Context, mark: str, item: dict) -> None:
    td = ctx.tasks
    if mark == "done":
        td.close_task(item["task_id"])
        if ctx.on_done:
            ctx.on_done(item["task_id"])
    elif mark == "tomorrow":
        td.set_due_date(item["task_id"], (ctx.today() + timedelta(days=1)).isoformat())
        if ctx.on_move:
            ctx.on_move(item["task_id"])
    elif mark == "drop":
        td.delete_task(item["task_id"])
    else:
        raise ValueError(mark)


def scan_photo(ctx: Context, vision: VisionFn, image: bytes, media_type: str,
               label_id: str | None = None) -> dict:
    hint_label = _find_label(ctx, None, label_id)
    result = vision(image, media_type, hint_label["manifest"] if hint_label else [])
    label = hint_label or _find_label(ctx, result.label_code, None)
    scan_id = ctx.store.new_id()
    record: dict = {
        "id": scan_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "label_id": label["id"] if label else None,
        "label_code_read": result.label_code,
        "by": ctx.user,
        "household_id": ctx.household,
        # Raw read-back, for debugging misreads. Not the notes: those are private to the journal.
        "vision": result.model_dump(exclude={"notes"}),
        "applied": [],
        "needs_confirmation": [],
        "ignored": [],
        "errors": [],
    }
    notes = (result.notes or "").strip()
    if not label:
        page = _keep_page(ctx, image, notes, scan_id, None, ctx.today().isoformat())
        if page:
            record["journal_id"] = page["id"]
        if not result.rows and page:
            record["message"] = "Saved to your journal. There was no printed label in the photo."
        else:
            record["errors"].append("Couldn't match the photo to a printed label. Pass label_id explicitly.")
        ctx.store.save_scan(record)
        return {**record, "notes": notes} if page else record

    by_row = {m["row"]: m for m in label["manifest"]}
    seen: set[int] = set()
    threshold = float(ctx.prefs.get("confidence", CONFIDENCE_THRESHOLD))
    auto_apply = bool(ctx.prefs.get("auto_apply", True))
    for rm in result.rows:
        item = by_row.get(rm.row)
        if item is None or rm.row in seen:
            record["ignored"].append({"row": rm.row, "mark": rm.mark, "reason": "not on this label"
                                      if item is None else "duplicate row"})
            continue
        seen.add(rm.row)
        if rm.mark == "none":
            continue
        entry = {**item, "mark": rm.mark, "confidence": round(rm.confidence, 2), "note": rm.note}
        # Drops always wait for a person, whatever the settings say.
        if rm.mark == "drop" or rm.confidence < threshold or not auto_apply:
            record["needs_confirmation"].append({**entry, "status": "pending"})
            continue
        try:
            _apply(ctx, rm.mark, item)
            record["applied"].append(entry)
        except Exception as exc:  # keep going; report per-row
            record["errors"].append(f"row {rm.row}: {exc}")
    day = label["created_at"][:10] if label.get("kind") == "today" and label.get("created_at") else ctx.today().isoformat()
    page = _keep_page(ctx, image, notes, scan_id, label["id"], day)
    if page:
        record["journal_id"] = page["id"]
    ctx.store.save_scan(record)  # the scan record is visible to the superadmin, so no notes in it
    return {**record, "notes": notes} if page else record


def _keep_page(ctx: Context, image: bytes, notes: str, scan_id: str, label_id: str | None, day: str) -> dict | None:
    """Keep the photo as a journal page when it has notes, or always in journal mode."""
    if ctx.save_page is None or not (notes or ctx.prefs.get("journal_mode", True)):
        return None
    return ctx.save_page(day=day, notes=notes, photo=image, scan_id=scan_id, label_id=label_id)


def confirm(ctx: Context, scan_id: str, decisions: dict[int, str]) -> dict:
    """decisions: {row: "confirm" | "skip"}. Only rows still pending are touched, so a
    double-tap in the app can't delete twice or re-apply a skipped row."""
    with ctx.store.edit_scan(scan_id) as record:
        for item in record["needs_confirmation"]:
            decision = decisions.get(item["row"])
            if item["status"] != "pending" or decision not in {"confirm", "skip"}:
                continue
            if decision == "skip":
                item["status"] = "skipped"
                continue
            try:
                _apply(ctx, item["mark"], item)
                item["status"] = "applied"
            except Exception as exc:
                item["status"] = "error"
                item["error"] = str(exc)
        return record
