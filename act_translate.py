#!/usr/bin/env python3
"""
act_translate.py
================
Translate a scraped BDLaw Markdown file (Bengali) to English using
facebook/nllb-200-distilled-1.3B via CTranslate2 int8.

The model (~2.6 GB) is downloaded once to the HuggingFace cache on first run.

Usage:
    python act_translate.py --act 927
    python act_translate.py --act 927 --device cpu
    python act_translate.py --act 927 --input my.md --output out.md
    python act_translate.py --act 927 --beam 5 --verbose
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import click
import ctranslate2
from huggingface_hub import snapshot_download
from transformers import AutoTokenizer

# ---------------------------------------------------------------------------
# Rich (optional)
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress,
        SpinnerColumn, TextColumn, TimeElapsedColumn,
    )
    _RICH = True
    console = Console(stderr=True)
except ImportError:
    _RICH = False
    console = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MODEL_ID   = "OpenNMT/nllb-200-distilled-1.3B-ct2-int8"
SRC_LANG   = "ben_Beng"
TGT_LANG   = "eng_Latn"
MAX_TOKENS = 350    # conservative ceiling — leaves decode headroom, reduces loops
MAX_DECODE = 512

# ---------------------------------------------------------------------------
# Legal glossary  (Fix 3)
# ---------------------------------------------------------------------------
# Applied BEFORE the model sees the text.  Longer / more specific phrases must
# appear before shorter ones that share substrings.
# Replacements are English anchors the model copies verbatim into output.

LEGAL_GLOSSARY: list[tuple[str, str]] = [
    # ── Section titles (long phrases first, before sub-term patterns) ─────────
    # Hardcoded because NLLB gets these consistently wrong without anchors.
    (r"সংক্ষিপ্ত শিরোনামা ও প্রবর্তন",           "Short Title and Commencement"),
    (r"অন্যান্য আইনের প্রয়োগ",                    "Application of Other Laws"),
    (r"পাবলিক প্লেস এবং পাবলিক পরিবহণে.+নিষিদ্ধ", "Prohibition of Smoking in Public Places and Public Transport"),
    (r"তামাকজাত দ্রব্যের বিজ্ঞাপন.+নিষিদ্ধ",      "Prohibition of Advertising of Tobacco Products and Control of Sponsorship"),
    (r"অটোমেটিক ভেন্ডিং মেশিন স্থাপন নিষিদ্ধ",    "Prohibition of Installation of Automatic Vending Machines"),
    (r"অপ্রাপ্ত বয়স্ক.+বিক্রয় নিষিদ্ধ",          "Prohibition of Sale of Tobacco Products to Minors"),
    (r"সীমানার মধ্যে তামাক.+বিক্রয় নিষিদ্ধ",      "Prohibition of Sale of Tobacco within Premises of Educational Institutions etc."),
    (r"কুম্ভি পাতা.+নিষিদ্ধ",                      "Prohibition of Kumbhi Leaf Bidi etc."),
    (r"আসক্তিমূলক দ্রব্য ব্যবহার নিষিদ্ধ",         "Prohibition of Use of Addictive Substances with Tobacco Products"),
    (r"সতর্কতামূলক নোটিশ প্রদর্শন",                 "Display of Warning Notices"),
    (r"কর্তৃত্বপ্রাপ্ত কর্মকর্তার ক্ষমতা",          "Powers of Authorized Officer"),
    (r"তামাকজাত দ্রব্যের প্যাকেটে.+মুদ্রণ",        "Printing of Health Warnings on Tobacco Product Packaging"),
    (r"স্ট্যান্ডার্ড প্যাকেজিং ব্যতীত.+বিধিনিষেধ", "Restrictions on Sale of Tobacco Products other than Standard Packaging"),
    (r"তামাকজাত দ্রব্য আমদানির.+তথ্য প্রদান",      "Provision of Ingredient Information when Importing Tobacco Products"),
    (r"তামাক.+ফসল উৎপাদন.+নিরুৎসাহিতকরণ",         "Discouragement of Production of Tobacco Crops"),
    (r"অপরাধ বিচারার্থ গ্রহণ এবং জামিনযোগ্য",       "Cognizance of Offences and Bail"),
    (r"কোম্পানী কর্তৃক অপরাধ সংঘটন",               "Offences by Companies"),
    (r"জাতীয় তামাক নিয়ন্ত্রণ সেল",                "National Tobacco Control Cell"),
    (r"বিধি প্রণয়নের ক্ষমতা",                      "Power to Make Rules"),
    (r"মূল পাঠ এবং ইংরেজীতে পাঠ",                  "Original Text and Authentic English Text"),
    (r"রহিতকরণ ও হেফাজত",                           "Repeal and Savings"),
    (r"সংজ্ঞা",                                      "Definitions"),
    (r"জনসেবক",                                      "Public Servants"),
    # ── Bengali vowel list labels (অ আ ই ঈ উ ঊ) → Latin (a b c d e f) ────────
    # Section 10(2) warning labels use Bengali vowel markers as sub-list items.
    (r"\(অ\)", "(a)"),
    (r"\(আ\)", "(b)"),
    (r"\(ই\)",  "(c)"),
    (r"\(ঈ\)",  "(d)"),
    (r"\(উ\)", "(e)"),
    (r"\(ঊ\)",  "(f)"),
    # ── Currency (before কারাদণ্ড entries) ───────────────────────────────────
    (r"টাকা\s+অর্থদণ্ডে\s+দণ্ডনীয়",  "Taka monetary fine shall be imposed"),
    (r"টাকা\s+অর্থদণ্ড",               "Taka monetary fine"),
    (r"অর্থদণ্ডে\s+দণ্ডনীয়",          "monetary fine shall be imposed"),
    (r"অর্থদণ্ড",                        "monetary fine"),
    (r"টাকা",                            "Taka"),
    # Imprisonment
    (r"বিনাশ্রম\s+কারাদণ্ড",           "simple imprisonment"),
    (r"কারাদণ্ড",                        "imprisonment"),
    # Persons / entities
    (r"জনসেবক",                          "Public Servant"),
    (r"কর্তৃত্বপ্রাপ্ত\s+কর্মকর্তা",   "Authorized Officer"),
    # Tobacco terms
    (r"বিড়ি",                            "bidi"),
    (r"তামাকজাত\s+দ্রব্য",             "tobacco products"),
    (r"তামাক",                           "tobacco"),
    (r"ধূমপান",                          "smoking"),
    (r"কুম্ভি\s+পাতা\S*",                  "Kumbhi leaf"),
    (r"টেন্ডু\s+পাতা\S*",                  "Tendu leaf"),
    # Legal status
    (r"বিলুপ্ত",                         "repealed"),
    (r"আমলযোগ্য",                        "Cognizable"),
    (r"জামিনযোগ্য",                      "Bailable"),
    # City names (prevent Rajasthan / Silet / Borishal mistakes)
    (r"রাজশাহী",                         "Rajshahi"),
    (r"সিলেট",                           "Sylhet"),
    (r"বরিশাল",                          "Barishal"),
    (r"গাজীপুর",                         "Gazipur"),
    (r"রংপুর",                           "Rangpur"),
    (r"ময়মনসিংহ",                        "Mymensingh"),
    (r"নারায়ণগঞ্জ",                     "Narayanganj"),
    (r"ঢাকা",                            "Dhaka"),
    (r"চট্টগ্রাম",                       "Chittagong"),
    (r"খুলনা",                           "Khulna"),
    # Misc legal
    (r"ইরেজী",                           "English"),
    (r"ইংরেজী",                          "English"),
    (r"সরকারী\s+গেজেট\S*",            "Official Gazette"),
    (r"সরকারি\s+গেজেট\S*",            "Official Gazette"),
    (r"উপ-ধারা",                         "sub-section"),
    (r"মহানগরী\s+পুলিশ\s+আইন",        "Metropolitan Police Act"),
]

_GLOSSARY_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile(pat), repl) for pat, repl in LEGAL_GLOSSARY
]


def apply_glossary(text: str) -> str:
    """Pre-substitute known legal terms before NLLB sees the text."""
    for pattern, replacement in _GLOSSARY_COMPILED:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Loop detector  (Fix 2)
# ---------------------------------------------------------------------------

def detect_loop(text: str, phrase_words: int = 6, max_repeats: int = 2) -> str:
    """
    Detect repetition loops in translated text and truncate at the point
    where a phrase of *phrase_words* words recurs more than *max_repeats* times.
    Appends a visible warning marker so the issue is obvious in the output.
    """
    words = text.split()
    n = len(words)
    if n < phrase_words * (max_repeats + 1):
        return text

    seen: dict[tuple[str, ...], int] = {}
    for i in range(n - phrase_words + 1):
        phrase = tuple(words[i : i + phrase_words])
        if phrase in seen:
            occurrences = sum(
                1 for j in range(i)
                if tuple(words[j : j + phrase_words]) == phrase
            )
            if occurrences >= max_repeats:
                truncated = " ".join(words[:i]).rstrip(".,;: ")
                return truncated + " [⚠ translation truncated: repetition loop detected]"
        else:
            seen[phrase] = i

    return text

# ---------------------------------------------------------------------------
# Pre-processing helpers  (annotation stripping, numeral normalisation)
# ---------------------------------------------------------------------------

# Bengali digits → ASCII digits (for section number normalisation)
_BN_TO_ARABIC = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# Bengali consonant subsection letters → Latin uppercase (ক→A, খ→B …)
_BN_SUBLETTER: dict[str, str] = {
    "ক": "A", "খ": "B", "গ": "C", "ঘ": "D", "ঙ": "E",
    "চ": "F", "ছ": "G", "জ": "H", "ঝ": "I", "ঞ": "J",
    "ট": "K", "ঠ": "L", "ড": "M", "ঢ": "N", "ণ": "O",
    "ত": "P", "থ": "Q", "দ": "R", "ধ": "S", "ন": "T",
}

# Matches "Section ৬ক:" / "Section ১০ক:" in heading text
_RE_SECTION_REF = re.compile(
    r"Section\s+([০-৯]+)([কখগঘঙচছজঝঞটঠডঢণতথদধন]?)"
)

# Matches inline Bengali annotation brackets: [Bengali text] (not [* * *])
# Used by BDLaw to mark amended provisions; we strip the brackets, keep content.
_RE_BN_ANNOTATION = re.compile(
    r"\[(?!\*\s*\*)([^\[\]]*[ঀ-৿][^\[\]]*)\]"
)

# Post-translate: fix subsection letters lowercased by NLLB e.g. "Section 6d" → "6D"
_RE_SECTION_LOWERCASE_SUB = re.compile(r"Section\s+(\d+)([a-z])(?=[\s:,.]|$)")


def normalize_section_refs(text: str) -> str:
    """
    Convert Bengali-numeral section references to Arabic before NLLB sees them.
    e.g. "Section ৬ক:" → "Section 6A:"  /  "Section ২:" → "Section 2:"
    """
    def _repl(m: re.Match) -> str:
        num = m.group(1).translate(_BN_TO_ARABIC)
        sub = _BN_SUBLETTER.get(m.group(2), "") if m.group(2) else ""
        return f"Section {num}{sub}"
    return _RE_SECTION_REF.sub(_repl, text)


def strip_bn_annotations(text: str) -> str:
    """
    Remove BDLaw-style amendment brackets [Bengali text] → text.
    Prevents the bracket from appearing literally in translated output and
    stops glossary patterns from being split mid-sentence by a stray '['.
    """
    return _RE_BN_ANNOTATION.sub(r"", text)


def fix_section_case(text: str) -> str:
    """
    Post-translate fix: NLLB sometimes lowercases subsection letters.
    "Section 6d:" → "Section 6D:"
    """
    return _RE_SECTION_LOWERCASE_SUB.sub(
        lambda m: f"Section {m.group(1)}{m.group(2).upper()}", text
    )


def preprocess(text: str) -> str:
    """
    Full pre-processing pipeline applied to Bengali text before NLLB inference.
      1. Strip inline amendment brackets [Bengali…]
      2. Normalise section references (Bengali digits → Arabic, ক→A …)
      3. Apply legal glossary substitutions
    """
    text = strip_bn_annotations(text)
    text = normalize_section_refs(text)
    text = apply_glossary(text)
    return text


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    if _RICH:
        logging.basicConfig(
            level=level, format="%(message)s",
            handlers=[RichHandler(console=console, show_path=False, markup=True)],
        )
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    return logging.getLogger("translate")


# ---------------------------------------------------------------------------
# Bengali detection
# ---------------------------------------------------------------------------

def has_bengali(text: str) -> bool:
    """True if *text* contains any Bengali Unicode character (U+0980–U+09FF)."""
    return any("\u0980" <= c <= "\u09FF" for c in text)


# ---------------------------------------------------------------------------
# Model wrapper
# ---------------------------------------------------------------------------

class NLLBTranslator:
    """
    Lazy-loaded NLLB-200 1.3B (CTranslate2 int8) Bengali → English translator.
    RTX 2060 (6 GB VRAM): int8 model uses ~1.5–2 GB — comfortable fit.
    """

    def __init__(self, device: str = "cuda", beam_size: int = 4,
                 log: Optional[logging.Logger] = None) -> None:
        self.device    = device
        self.beam_size = beam_size
        self.log       = log or logging.getLogger("translate")
        self._ct2: Optional[ctranslate2.Translator] = None
        self._tok = None

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self._ct2 is not None:
            return
        self.log.info("Fetching model weights: [bold]%s[/bold]", MODEL_ID)
        model_path = snapshot_download(MODEL_ID)
        self.log.info("Loading CTranslate2 engine on [bold]%s[/bold] …", self.device.upper())

        self._ct2 = self._init_translator(model_path)
        self._tok = AutoTokenizer.from_pretrained(model_path, src_lang=SRC_LANG)
        self.log.info("Model ready ✓  (device=%s)", self.device.upper())

    def _init_translator(self, model_path: str) -> ctranslate2.Translator:
        """
        Try to create a CTranslate2 Translator, probing compute types and
        falling back gracefully when CUDA libraries are missing.

        Probe order for CUDA:
          1. int8_float16  — fastest on RTX 2060, needs cuBLAS
          2. int8          — slightly slower, still needs cuBLAS
          3. CPU int8      — automatic fallback when libcublas.so.N not found

        Nobara / Fedora fix if you want native CUDA speed:
          sudo find /usr /opt -name 'libcublas.so.12' 2>/dev/null
          # then add that directory to /etc/ld.so.conf.d/cuda.conf + ldconfig
        """
        _CUDA_COMPUTE_TYPES = ["int8_float16", "int8"]
        _CPU_COMPUTE_TYPE   = "int8"
        _CUDA_LIB_ERRORS    = ("libcublas", "libcuda", "cannot be loaded", "not found")

        def _is_cuda_lib_error(e: RuntimeError) -> bool:
            return any(kw in str(e).lower() for kw in _CUDA_LIB_ERRORS)

        if self.device == "cuda":
            for compute_type in _CUDA_COMPUTE_TYPES:
                try:
                    self.log.debug("Trying compute_type=%s on CUDA …", compute_type)
                    return ctranslate2.Translator(
                        model_path,
                        device="cuda",
                        inter_threads=1,
                        intra_threads=4,
                        compute_type=compute_type,
                    )
                except RuntimeError as e:
                    if _is_cuda_lib_error(e):
                        self.log.warning(
                            "CUDA lib error with compute_type=%s: %s", compute_type, e
                        )
                        continue   # try next compute type
                    raise          # unrelated error — propagate

            # All CUDA attempts failed — fall back to CPU
            self.log.warning(
                "⚠️  All CUDA compute types failed (libcublas not found?).\n"
                "   Falling back to CPU — translation will be slower (~10–20×).\n"
                "   To fix on Nobara: sudo find /usr /opt -name 'libcublas.so.*' 2>/dev/null\n"
                "   Then: echo '<that dir>' | sudo tee /etc/ld.so.conf.d/cuda.conf && sudo ldconfig"
            )
            self.device = "cpu"   # update so downstream logging is correct

        # CPU path (either requested directly or fallen back to)
        return ctranslate2.Translator(
            model_path,
            device="cpu",
            inter_threads=4,
            intra_threads=4,
            compute_type=_CPU_COMPUTE_TYPE,
        )

    # ------------------------------------------------------------------
    def _run_batch(self, token_seqs: list[list[str]]) -> list[str]:
        """Send a batch of token lists through CTranslate2, return strings."""
        results = self._ct2.translate_batch(
            token_seqs,
            target_prefix=[[TGT_LANG]] * len(token_seqs),
            beam_size=self.beam_size,
            max_decoding_length=MAX_DECODE,
        )
        output: list[str] = []
        for res in results:
            out_tokens = res.hypotheses[0][1:]   # drop TGT_LANG prefix token
            ids = self._tok.convert_tokens_to_ids(out_tokens)
            output.append(self._tok.decode(ids, skip_special_tokens=True))
        return output

    # ------------------------------------------------------------------
    def _tokenise(self, text: str) -> list[str]:
        return self._tok.convert_ids_to_tokens(self._tok.encode(text))

    # ------------------------------------------------------------------
    # Fix 1: robust chunking ─────────────────────────────────────────────
    def _pack_parts(self, parts: list[str]) -> list[str]:
        """Greedily pack split parts into MAX_TOKENS chunks."""
        chunks: list[str] = []
        current = ""
        for part in parts:
            candidate = (current + " " + part).strip() if current else part
            if len(self._tokenise(candidate)) > MAX_TOKENS and current:
                chunks.append(current)
                current = part
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    def _force_split(self, text: str) -> list[str]:
        """Last-resort word-boundary split to guarantee ≤ MAX_TOKENS per chunk."""
        words = text.split()
        chunks: list[str] = []
        current_words: list[str] = []
        for word in words:
            candidate = " ".join(current_words + [word])
            if len(self._tokenise(candidate)) > MAX_TOKENS and current_words:
                chunks.append(" ".join(current_words))
                current_words = [word]
            else:
                current_words.append(word)
        if current_words:
            chunks.append(" ".join(current_words))
        return chunks or [text]

    def _chunk_text(self, text: str) -> list[str]:
        """
        Split *text* into chunks each ≤ MAX_TOKENS.
        Fallback chain:
          1. Danda / newline  (।  ৷  \n)
          2. Comma            (,)
          3. Semicolon        (;)
          4. Force-split at word boundary (guarantees limit)
        """
        if len(self._tokenise(text)) <= MAX_TOKENS:
            return [text]

        for pattern in [
            r"(?<=[।৷])\s*",
            r"\n",
            r"(?<=,)\s*",
            r"(?<=;)\s*",
        ]:
            parts = [p for p in re.split(pattern, text) if p.strip()]
            if len(parts) > 1:
                packed = self._pack_parts(parts)
                if all(len(self._tokenise(c)) <= MAX_TOKENS for c in packed):
                    return packed

        return self._force_split(text)

    # ------------------------------------------------------------------
    def translate(self, text: str) -> str:
        """
        Translate a single Bengali string → English.
        Pipeline: glossary substitution → chunked NLLB inference → loop detection.
        """
        if not text.strip() or not has_bengali(text):
            return text
        self._load()

        # Pre-processing: strip brackets, normalise section refs, apply glossary
        prepped = preprocess(text)

        # Short-circuit: glossary may have resolved everything
        if not has_bengali(prepped):
            return fix_section_case(prepped)

        # Fix 1: chunk so nothing over MAX_TOKENS reaches the model
        chunks = self._chunk_text(prepped)
        token_seqs = [self._tokenise(c) for c in chunks]
        translated_parts = self._run_batch(token_seqs)
        raw = " ".join(translated_parts)

        # Fix 2: detect and truncate repetition loops
        raw = detect_loop(raw)

        # Post-processing: fix lowercased subsection letters (6d → 6D)
        return fix_section_case(raw)

    # ------------------------------------------------------------------
    def translate_batch(self, texts: list[str]) -> list[str]:
        """
        Translate a list of strings in one GPU batch.
        Each string goes through glossary → chunking → loop detection.
        Strings without Bengali are passed through unchanged.
        """
        if not texts:
            return []
        self._load()

        output = list(texts)

        for i, text in enumerate(texts):
            if text.strip() and has_bengali(text):
                output[i] = self.translate(text)   # reuse full pipeline

        return output


# ---------------------------------------------------------------------------
# Markdown structure — segment extraction + reconstruction
# ---------------------------------------------------------------------------

RE_HEADING_LINK  = re.compile(r"^(#{1,6}\s*)\[([^\]]+)\]\(([^)]*)\)(.*)")
RE_NUMBERED_ITEM = re.compile(r"^(\d+\.\s+)(.*)", re.DOTALL)
RE_OL_WRAPPER    = re.compile(r"(<ol[^>]*>)(.*?)(</ol>)", re.DOTALL)
RE_LI_CONTENT    = re.compile(r"<li>(.*?)</li>", re.DOTALL)


@dataclass
class Segment:
    """
    A Bengali string extracted from one line, plus a callable that puts the
    translated string back into the original Markdown structure.
    """
    text: str
    reconstruct: Callable[[str], str]


def extract_segments(line: str) -> list[Segment]:
    """
    Decompose a Markdown line into translatable segments.
    Returns [] if the line has no Bengali content.

    Handles:
      - Heading with hyperlink : ##### [Bengali text](url)
      - Numbered list item     : 1. Bengali text
      - HTML list block        : <ol ...><li>Bengali</li>...</ol>
      - Plain paragraph        : Bengali text
    """
    if not line.strip() or not has_bengali(line):
        return []

    # ── Heading with link ──────────────────────────────────────────────
    m = RE_HEADING_LINK.match(line)
    if m:
        prefix, text, url, trailing = m.group(1), m.group(2), m.group(3), m.group(4)
        def _rebuild_heading(t: str, p=prefix, u=url, tr=trailing) -> str:
            return f"{p}[{t}]({u}){tr}"
        return [Segment(text=text, reconstruct=_rebuild_heading)]

    # ── <ol>…</ol> block: one segment per <li> ─────────────────────────
    if RE_OL_WRAPPER.search(line):
        li_matches = list(RE_LI_CONTENT.finditer(line))
        if not li_matches:
            return []

        # Capture the original full line for reconstruction
        # Build a list of (span, original_content) so we can substitute in order
        original = line
        segments: list[Segment] = []
        span_contents: list[tuple[tuple[int, int], str]] = [
            (m.span(1), m.group(1)) for m in li_matches
        ]

        for idx, (span, content) in enumerate(span_contents):
            if not has_bengali(content):
                continue

            def _rebuild_li(translated: str, _idx=idx, _spans=span_contents, _orig=original) -> str:
                # Re-read original and apply all substitutions up to this point.
                # Because segments are applied one at a time, we rebuild from
                # the partially-mutated state stored in a mutable container.
                # Simpler: we always work on the full rebuilt string.
                # Since Segment.reconstruct receives only one translated string,
                # we store a reference to a shared "current line" list.
                # Instead: all li segments share one closure over a list.
                # → handled via _OLState below.
                return translated   # placeholder; see _OLState

            segments.append(Segment(text=content, reconstruct=_rebuild_li))

        # Replace the placeholder reconstructors with a shared-state approach
        state = _OLState(original, li_matches)
        final_segs: list[Segment] = []
        for li_idx, (span, content) in enumerate(span_contents):
            if not has_bengali(content):
                continue
            final_segs.append(Segment(
                text=content,
                reconstruct=state.make_reconstructor(li_idx),
            ))
        return final_segs

    # ── Numbered list item: "1. text" ─────────────────────────────────
    m = RE_NUMBERED_ITEM.match(line)
    if m:
        prefix, content = m.group(1), m.group(2)
        def _rebuild_numbered(t: str, p=prefix) -> str:
            return p + t
        return [Segment(text=content, reconstruct=_rebuild_numbered)]

    # ── Plain paragraph ────────────────────────────────────────────────
    return [Segment(text=line, reconstruct=lambda t: t)]


class _OLState:
    """
    Shared state for reconstructing an <ol> line where multiple <li>
    items are translated independently.  Each reconstructor writes its
    translated content into `self.parts` then returns the full line.
    """

    def __init__(self, original: str, li_matches: list[re.Match]) -> None:
        self.original   = original
        self.li_matches = li_matches
        # Start with original content per <li>
        self.parts: list[str] = [m.group(1) for m in li_matches]

    def make_reconstructor(self, li_idx: int) -> Callable[[str], str]:
        def _rebuild(translated: str) -> str:
            self.parts[li_idx] = translated
            # Rebuild full line by substituting all parts
            result = self.original
            offset = 0
            for i, m in enumerate(self.li_matches):
                start = m.start(1) + offset
                end   = m.end(1)   + offset
                new_content = self.parts[i]
                result  = result[:start] + new_content + result[end:]
                offset += len(new_content) - (m.end(1) - m.start(1))
            return result
        return _rebuild


# ---------------------------------------------------------------------------
# Translation pass
# ---------------------------------------------------------------------------

def translate_markdown(
    src: Path,
    dst: Path,
    translator: NLLBTranslator,
    batch_size: int,
    log: logging.Logger,
) -> None:
    raw_lines = src.read_text(encoding="utf-8").splitlines()
    log.info("Input: %s (%d lines)", src, len(raw_lines))

    # Pass 1 — extract all segments from all lines
    # line_segments[i] = list of Segment objects for line i
    line_segments: list[list[Segment]] = [extract_segments(l) for l in raw_lines]

    # Flatten into a single ordered list for batching
    flat_segs: list[tuple[int, int, Segment]] = []   # (line_idx, seg_idx, seg)
    for li, segs in enumerate(line_segments):
        for si, seg in enumerate(segs):
            flat_segs.append((li, si, seg))

    log.info(
        "Segments to translate: %d  (across %d lines)",
        len(flat_segs),
        sum(1 for s in line_segments if s),
    )

    # Pass 2 — batch-translate
    translated_texts: list[str] = []
    total = len(flat_segs)

    def _do_batch(batch: list[tuple[int, int, Segment]]) -> list[str]:
        return translator.translate_batch([s.text for _, _, s in batch])

    if _RICH:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Translating …", total=total)
            for i in range(0, total, batch_size):
                chunk = flat_segs[i : i + batch_size]
                translated_texts.extend(_do_batch(chunk))
                progress.advance(task, len(chunk))
    else:
        for i in range(0, total, batch_size):
            chunk = flat_segs[i : i + batch_size]
            translated_texts.extend(_do_batch(chunk))
            log.info("  %d / %d segments", min(i + batch_size, total), total)

    # Pass 3 — apply translations back through reconstruct functions
    # Build per-line translated output
    output_lines = list(raw_lines)   # start with originals
    flat_idx = 0
    for li, segs in enumerate(line_segments):
        if not segs:
            continue
        current_line = raw_lines[li]
        for seg in segs:
            translated = translated_texts[flat_idx]
            flat_idx += 1
            current_line = seg.reconstruct(translated)
        output_lines[li] = current_line

    dst.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    log.info("✅  Saved → %s  (%d chars)", dst, dst.stat().st_size)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--act", "act_number", required=True,
    help="Act number — reads act-<number>.md in the current directory.",
)
@click.option(
    "--input", "-i", "input_file", default=None,
    help="Override input path (default: act-<number>.md).",
)
@click.option(
    "--output", "-o", "output_file", default=None,
    help="Override output path (default: act-<number>-en.md).",
)
@click.option(
    "--device", default="cuda", show_default=True,
    type=click.Choice(["cuda", "cpu"]),
    help="Inference device.  cpu works but is ~10–20× slower.",
)
@click.option(
    "--beam", "beam_size", default=4, show_default=True, type=int,
    help="Beam size for decoding.  Higher = marginally better quality, slower.",
)
@click.option(
    "--batch-size", "batch_size", default=16, show_default=True, type=int,
    help="Number of segments to send to the GPU per batch.",
)
@click.option("--verbose", "-v", is_flag=True, default=False)
def main(
    act_number: str,
    input_file: Optional[str],
    output_file: Optional[str],
    device: str,
    beam_size: int,
    batch_size: int,
    verbose: bool,
) -> None:
    """Translate a scraped BDLaw Markdown file from Bengali to English."""
    log = setup_logging(verbose)

    src = Path(input_file)  if input_file  else Path(f"act-{act_number}.md")
    dst = Path(output_file) if output_file else Path(f"act-{act_number}-en.md")

    if not src.exists():
        log.error("Input file not found: %s  (run bdlaw_scraper.py first)", src)
        sys.exit(1)

    t = NLLBTranslator(device=device, beam_size=beam_size, log=log)
    try:
        translate_markdown(src, dst, t, batch_size=batch_size, log=log)
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()