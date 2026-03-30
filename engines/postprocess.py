"""
engines/postprocess.py — 통합 후처리 파이프라인

레이어 구조:
  Layer 1 (universal)      — 모든 엔진·문서 유형에 공통 적용
  Layer 2 (doc_type)       — 문서 종류별 규칙 (journal_paper / report / admin_doc)
  Layer 3 (engine_quirks)  — 엔진별 특수 수정 (marker / pymupdf4llm / docling / mineru)

주요 API:
  postprocess(md_text, engine=None, doc_type="journal_paper") -> str
"""

import re
from collections import Counter


# ═══════════════════════════════════════════════════════════════════
# LAYER 1 — UNIVERSAL
# ═══════════════════════════════════════════════════════════════════

# Adobe Symbol encoding → Unicode 변환 (PUA 영역)
_SYMBOL_PUA: dict[str, str] = {
    '\uF041': 'Α', '\uF042': 'Β', '\uF043': 'Χ', '\uF044': 'Δ', '\uF045': 'Ε',
    '\uF046': 'Φ', '\uF047': 'Γ', '\uF048': 'Η', '\uF049': 'Ι', '\uF04A': 'ϑ',
    '\uF04B': 'Κ', '\uF04C': 'Λ', '\uF04D': 'Μ', '\uF04E': 'Ν', '\uF04F': 'Ο',
    '\uF050': 'Π', '\uF051': 'Θ', '\uF052': 'Ρ', '\uF053': 'Σ', '\uF054': 'Τ',
    '\uF055': 'Υ', '\uF056': 'ς', '\uF057': 'Ω', '\uF058': 'Ξ', '\uF059': 'Ψ',
    '\uF05A': 'Ζ',
    '\uF061': 'α', '\uF062': 'β', '\uF063': 'χ', '\uF064': 'δ', '\uF065': 'ε',
    '\uF066': 'φ', '\uF067': 'γ', '\uF068': 'η', '\uF069': 'ι', '\uF06A': 'φ',
    '\uF06B': 'κ', '\uF06C': 'λ', '\uF06D': 'μ', '\uF06E': 'ν', '\uF06F': 'ο',
    '\uF070': 'π', '\uF071': 'θ', '\uF072': 'ρ', '\uF073': 'σ', '\uF074': 'τ',
    '\uF075': 'υ', '\uF077': 'ω', '\uF078': 'ξ', '\uF079': 'ψ', '\uF07A': 'ζ',
}

_LIGATURES: dict[str, str] = {
    '\ufb00': 'ff', '\ufb01': 'fi', '\ufb02': 'fl',
    '\ufb03': 'ffi', '\ufb04': 'ffl',
}

_SPECIAL_SPACES: list[tuple[str, str]] = [
    ('\u00A0', ' '), ('\u2008', ' '), ('\u2006', ' '),
    ('\u2009', ' '), ('\u200A', ' '), ('\u00AD', ''),
]

# 알려진 손상 기호 패턴 (특정 폰트 인코딩 문제)
_KNOWN_CORRUPTIONS: dict[str, str] = {
    'ţ šŢ Ⴞ š -v Ţ Ⴟ': '³/[12(1−ν²)]',
}


def _fix_text_encoding(md: str) -> str:
    """PUA 기호·리가처·HTML 엔티티·특수 공백 수정."""
    # HTML entities
    _HTML_ENTITIES = {'&amp;': '&', '&lt;': '<', '&gt;': '>',
                      '&nbsp;': ' ', '&quot;': '"', '&#39;': "'"}
    for e, c in _HTML_ENTITIES.items():
        md = md.replace(e, c)
    # PUA symbols
    for pua, ch in _SYMBOL_PUA.items():
        md = md.replace(pua, ch)
    # Ligatures
    for lig, rep in _LIGATURES.items():
        md = md.replace(lig, rep)
    # Special spaces & soft hyphens
    for bad, good in _SPECIAL_SPACES:
        md = md.replace(bad, good)
    # HTML span/sup/sub tags
    md = re.sub(r'<span[^>]*>', '', md)
    md = re.sub(r'</span>', '', md)
    md = re.sub(r'<sup>([^<]+)</sup>', r'^\1', md)
    md = re.sub(r'<sub>([^<]+)</sub>', r'_\1', md)
    # Known corrupted symbol patterns
    for corrupted, fixed in _KNOWN_CORRUPTIONS.items():
        md = md.replace(corrupted, fixed)
    return md


def _remove_publisher_watermarks(md: str) -> str:
    """출판사 워터마크·저작권 배너 제거 (Wiley, Elsevier, ACS, Springer, Nature 등)."""
    # Wiley download line: 12345678, 2024, 10, Downloaded from...
    md = re.sub(
        r'^\d{8},\s+\d{4},\s+\d+,\s+Downloaded from https://onlinelibrary\.wiley\.com.*$',
        '', md, flags=re.MULTILINE
    )
    # Wiley domain inline markdown link
    md = re.sub(
        r'\[www\.(advancedsciencenews|small-structures|advmat)\.[a-z]+\]\(http[^\)]+\)\s*'
        r'(\[www\.[^\]]+\]\(http[^\)]+\))?',
        '', md, flags=re.IGNORECASE
    )
    # Wiley / ACS standalone domain lines
    md = re.sub(
        r'^(www\s*\.\s*advancedsciencenews\s*\.\s*com|www\s*\.\s*small-structures\s*\.\s*com|'
        r'www\s*\.\s*advmat\s*\.\s*de|www\s*\.\s*wiley\.com|'
        r'pubs\.acs\.org|dx\.doi\.org/10\.1021)[^\n]*$',
        '', md, flags=re.MULTILINE | re.IGNORECASE
    )
    # Elsevier banner
    md = re.sub(
        r'^(© \d{4}.*Elsevier.*$|.*Download.*from.*ScienceDirect.*$)',
        '', md, flags=re.MULTILINE
    )
    # Journal structure banner (SMQII structure:)
    md = re.sub(
        r'^[A-Z]{3,6}\s*(structure|format|template)[:]*\s*$',
        '', md, flags=re.MULTILINE | re.IGNORECASE
    )
    # Generic URL-only lines
    md = re.sub(r'^[^\n\w]*(?:\*+|\[)*www\.[a-z\-]+\.[a-z]{2,}[^\n]*$', '', md, flags=re.MULTILINE)
    # Copyright lines
    md = re.sub(r'^©\s*\d{4}[^\n]*$', '', md, flags=re.MULTILINE)
    md = re.sub(r'^\*?\*?DOI:\s*10\.\d{4,}/\S+\*?\*?\s*$', '', md, flags=re.MULTILINE)
    md = re.sub(r'^[^\n]*\borcid\.org/\d{4}-\d{4}-\d{4}-\d{4}[^\n]*$',
                '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^[^\n]*\bORCID\s+identification\s+number[^\n]*$',
                '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^[^\n]*Creative\s+Commons[^\n]*$', '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^\s*©\s*\d{4}\s+The\s+Author[^\n]*\n?',
                '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^Copyright\s*©\s*\d{4}[^\n]*\n', '', md, flags=re.MULTILINE | re.IGNORECASE)
    # Science Advances / AAAS 저널 보일러플레이트 제거 (단독 줄, bold/italic 포함)
    md = re.sub(r'^Science\s+Advances\s*$', '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^_Science\s+Advances_[^\n]*$', '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^View\s+the\s+article\s+online\b.*$', '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^\*+View\s+the\s+article\s+online\b[^\n]*$', '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^\*+Permissions\*+\s*$', '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^Permissions\s*$', '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^\[Use\s+of\s+this\s+article\s+is\s+subject\b[^\n]*$', '', md, flags=re.MULTILINE | re.IGNORECASE)
    md = re.sub(r'^https?://www\.science\.org/[^\n]*$', '', md, flags=re.MULTILINE)
    md = re.sub(r'^(?:NW,\s*)?Washington,\s*DC\s+\d{5}\b[^\n]*$', '', md, flags=re.MULTILINE)
    # Spaced-out journal header: "S c i e n c e  A d v a n c e s" (heading 포함)
    md = re.sub(r'^(?:#{1,3}\s+)?[A-Z]\s[a-z]\s[a-z][A-Za-z\s|]+$', '', md, flags=re.MULTILINE)
    # ALL CAPS spaced journal header: "SCIENCE ADVANCES | RESEARCH ARTICLE" (heading 포함)
    md = re.sub(r'^(?:#{1,3}\s+)?[A-Z][A-Z \t]+\|\s*[A-Z][A-Z \t]+$', '', md, flags=re.MULTILINE)
    # Footer page info: "... (3 of 10) © 2024 ..."
    md = re.sub(r'^.*\(\d+ of \d+\)\s*©\s*\d{4}.*$', '', md, flags=re.MULTILINE)

    # 저자 기여도/이메일 각주가 본문에 혼입된 경우 제거 (Docling 레이아웃 병합 아티팩트)
    # "7 These authors contributed equally: A, B. e-mail: x@y.com; z@w.com spectrum, most..."
    md = re.sub(
        r'\d+\s+These\s+authors\s+contributed\s+equally:[^.]+\.\s+e-?mail:\s*[\w.\-@]+(?:\s*;\s*[\w.\-@]+)*\s*',
        '',
        md,
        flags=re.IGNORECASE
    )

    # Repeated paragraph removal (URL/copyright 반복 단락)
    _META_SIGNALS = re.compile(
        r'https?://|www\.[a-z\-]+\.[a-z]{2,}|©'
        r'|\d{4,7}\s*\(\d+\s*of\s*\d+\)'
        r'|Downloaded from|published by|All rights reserved'
        r'|licensee American Association'
        r'|Use of this article is subject',
        re.IGNORECASE
    )

    def _para_key(p: str) -> str:
        k = re.sub(r'\d+\s*of\s*\d+', '', p)
        k = re.sub(r'\b\d{4,7}\b', '', k)
        return re.sub(r'[\s\*_\[\]`#>\(\)]', '', k).lower()

    paras = md.split('\n\n')
    para_counts = Counter(_para_key(p) for p in paras if p.strip())
    filtered = []
    for p in paras:
        key = _para_key(p)
        content_lines = [ln for ln in p.strip().split('\n') if ln.strip()]
        if para_counts[key] >= 2 and len(content_lines) <= 4 and _META_SIGNALS.search(p):
            continue
        filtered.append(p)
    return '\n\n'.join(filtered)


def _normalize_spaces(md: str) -> str:
    """다중 공백 정규화 (코드블록·표 셀 제외) + µ 단위 공백 수정."""
    in_code = False
    result = []
    for line in md.split('\n'):
        if line.startswith('```'):
            in_code = not in_code
        if not in_code and not line.startswith('|'):
            line = re.sub(r' {2,}', ' ', line)
        result.append(line)
    md = '\n'.join(result)
    # µ 단위 앞 공백 제거: "µ m" → "µm"
    md = re.sub(r'μ ([mMlLsSnNgGΩ])(?=[^a-zA-Z])', r'μ\1', md)
    return md


def _normalize_citations(md: str) -> str:
    """인용 형식 정규화."""
    md = re.sub(r'\[(\d+)\]\s*\[([–\-])\]\s*\[(\d+)\]', r'[\1–\3]', md)
    md = re.sub(r'\[(\d+)\]\s*[–\-]\s*\[(\d+)\]', r'[\1–\2]', md)
    md = re.sub(r'\[(\d+)\]\s*\[,\]\s*\[(\d+)\]', r'[\1,\2]', md)
    md = re.sub(r'\[\[(\d+)\]\]', r'[\1]', md)
    md = re.sub(r'\[\[\]\[(.*?)\]\[\]\]', r'[\1]', md)
    # (n) 앞뒤 공백 제거
    md = re.sub(r'\(\s+(\d+(?:\s*[-–]\s*\d+)*(?:,\s*\d+)*)\s+\)', r'(\1)', md)
    return md


def _fix_mid_sentence_breaks(text: str) -> str:
    """문장 중간에 빈 줄로 끊어진 경우를 수정 (소문자로 이어지는 단락)."""
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (i + 2 < len(lines)
                and lines[i + 1].strip() == ''
                and lines[i + 2]
                and not lines[i + 2].startswith('#')
                and not lines[i + 2].startswith('!')
                and not lines[i + 2].startswith('$')
                and not lines[i + 2].startswith('**')
                and not lines[i + 2].startswith('>')
                and not lines[i + 2].startswith('---')
                and line.strip()
                and not line.startswith('#')
                and not line.startswith('!')
                and not line.startswith('[')
                and re.search(r'[a-z,]$', line.strip())
                and re.match(r'[a-z]', lines[i + 2].strip())):
            result.append(line + ' ' + lines[i + 2].strip())
            i += 3
        else:
            result.append(line)
            i += 1
    return '\n'.join(result)


def _fix_blank_lines(md: str) -> str:
    """3개 이상 연속 빈줄 → 2개."""
    return re.sub(r'\n{3,}', '\n\n', md)


def _apply_universal(md: str) -> str:
    """Layer 1: 범용 규칙 적용."""
    md = _fix_text_encoding(md)
    md = _remove_publisher_watermarks(md)
    md = _normalize_spaces(md)
    md = _normalize_citations(md)
    md = _fix_mid_sentence_breaks(md)
    md = _fix_blank_lines(md)
    return md


# ═══════════════════════════════════════════════════════════════════
# LAYER 2 — DOC TYPE: journal_paper
# ═══════════════════════════════════════════════════════════════════

_GREEK_TO_LATEX: dict[str, str] = {
    'ν': r'\nu', 'ε': r'\varepsilon', 'θ': r'\theta', 'σ': r'\sigma',
    'μ': r'\mu', 'α': r'\alpha', 'β': r'\beta', 'γ': r'\gamma',
    'δ': r'\delta', 'φ': r'\phi', 'ψ': r'\psi', 'ρ': r'\rho',
    'λ': r'\lambda', 'ω': r'\omega', 'η': r'\eta', 'κ': r'\kappa',
    'χ': r'\chi', 'ξ': r'\xi',
    '𝜈': r'\nu', '𝜃': r'\theta', '𝜀': r'\varepsilon', 'ɛ': r'\varepsilon',
}

# 유니코드 첨자 → LaTeX 변환
_UNICODE_SUB = str.maketrans('₀₁₂₃₄₅₆₇₈₉ₐₑₒₓ', '0123456789aeox')
_UNICODE_SUP = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻', '0123456789+-')

_SECTION_KEYWORDS = {
    'Conclusion', 'Conclusions', 'Experimental Section', 'Experimental',
    'Supporting Information', 'Acknowledgements', 'Acknowledgments',
    'Conflict of Interest', 'Data Availability Statement', 'Author Contributions',
    'Keywords', 'References',
}


def _convert_bold_headings(md: str) -> str:
    """**N. Section** → ## N. Section (pymupdf4llm 등에서 heading이 bold로 출력되는 문제 수정)."""
    # 두 줄 소제목 병합: **2.4. Title...**\n**continuation**
    md = re.sub(
        r'(\*\*\d+\.\d+\.?\s+[^*\n]+)\*\*\n\*\*([^*\n]+)\*\*',
        r'\1 \2**', md
    )
    lines = md.split('\n')
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#'):
            clean = re.sub(r'\*\*([^*]+)\*\*', r'\1', stripped)
            result.append(clean)
            continue
        m_full = re.match(r'^\*\*(.+?)\*\*\s*$', stripped)
        if not m_full:
            result.append(line)
            continue
        content = m_full.group(1).strip()
        content_plain = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)
        # 저널 배너 제거 (ALL-CAPS 스페이싱, 발행사 구분자)
        if re.match(r'^(RESEARCH\s+ARTICLE|ENGINEERING|APPLIED\s+SCIENCES|'
                    r'[A-Z]\s[A-Z]\s[A-Z])', content_plain):
            result.append('')
            continue
        # Science Advances / 2-컬럼 저널 ALL-CAPS 섹션 → ## 헤딩 변환
        _CAPS_SECTIONS = re.compile(
            r'^(ABSTRACT|INTRODUCTION|RESULTS|DISCUSSION|CONCLUSIONS?|'
            r'METHODS?|EXPERIMENTAL|MATERIALS\s+AND\s+METHODS|'
            r'MATERIALS\s+AND|SUPPLEMENTARY|ACKNOWLEDGEMENTS?|'
            r'AUTHOR\s+CONTRIBUTIONS?|DATA\s+AVAILABILITY|'
            r'COMPETING\s+INTERESTS?|FUNDING)\b',
            re.IGNORECASE
        )
        if _CAPS_SECTIONS.match(content_plain):
            title = content_plain.strip()
            result.append(f'## {title}')
            continue
        # **N. Section** → ## N. Section
        m_sec = re.match(r'^(\d+)\.\s+(.+)$', content_plain)
        if m_sec:
            result.append(f'## {m_sec.group(1)}. {m_sec.group(2)}')
            continue
        # **N.M. Subsection** → ### N.M. Subsection
        m_subsec = re.match(r'^(\d+\.\d+\.?)\s+(.+)$', content_plain)
        if m_subsec:
            result.append(f'### {m_subsec.group(1)} {m_subsec.group(2)}')
            continue
        # 알려진 섹션 키워드 → ##
        if any(kw.lower() in content_plain.lower() for kw in _SECTION_KEYWORDS):
            result.append(f'## {content_plain}')
            continue
        # Figure N. 독립 bold → 제거 (inject_figure_captions에서 처리)
        if re.match(r'^(Figure|Fig\.)\s+\d+[\.\|]', content_plain):
            result.append('')
            continue
        result.append(line)
    return '\n'.join(result)


def _normalize_headings(md: str) -> str:
    """# N. Section → ## N. Section (heading level 정규화)."""
    lines = md.split('\n')
    result = []
    for line in lines:
        m = re.match(r'^#\s+(\d+)\.\s+(.+)$', line)
        if m:
            result.append(f'## {m.group(1)}. {m.group(2)}')
            continue
        m = re.match(r'^#\s+(\d+\.\d+\.?)\s+(.+)$', line)
        if m:
            result.append(f'### {m.group(1)} {m.group(2)}')
            continue
        result.append(line)
    return '\n'.join(result)


def _format_abstract(md: str) -> str:
    """Abstract 단락 과도한 bold 제거."""
    paras = md.split('\n\n')
    title_idx = None
    for i, p in enumerate(paras):
        if re.match(r'^#\s+', p.strip()):
            title_idx = i
            break
    if title_idx is None:
        return md
    for i in range(title_idx + 1, min(title_idx + 20, len(paras))):
        p = paras[i]
        stripped = p.strip()
        if re.match(r'^#{1,4}\s', stripped):
            break
        plain_len = len(re.sub(r'[\*_]', '', stripped))
        if plain_len < 80:
            continue
        if not stripped.startswith('**'):
            continue
        bold_count = len(re.findall(r'\*\*[^*]+\*\*', stripped))
        if bold_count < 2:
            continue
        clean = re.sub(r'_\*\*([^*]+)\*\*_', r'_\1_', stripped)
        clean = re.sub(r'\*\*([^*]*)\*\*', r'\1', clean)
        clean = re.sub(r'\*\*', '', clean)
        paras[i] = f'**Abstract**\n\n{clean}'
        break
    return '\n\n'.join(paras)


def _merge_column_linebreaks(md: str) -> str:
    """2단 PDF 줄바꿈 병합."""
    lines = md.split('\n')
    result = []
    i = 0

    def _is_protected(line: str) -> bool:
        s = line.strip()
        return (not s
                or s.startswith('#')
                or s.startswith('- ')
                or s.startswith('* ')
                or re.match(r'^\d+\.', s)
                or s.startswith('|')
                or s.startswith('>')
                or s.startswith('![')
                or s.startswith('$$')
                or s.startswith('**Authors:**')
                or s.startswith('**Affiliations:**')
                or re.match(r'^\*\*\d+\.', s))

    while i < len(lines):
        line = lines[i]
        if _is_protected(line):
            result.append(line)
            i += 1
            continue
        while (i + 1 < len(lines)
               and lines[i + 1].strip()
               and not _is_protected(lines[i + 1])):
            line = line.rstrip() + ' ' + lines[i + 1].lstrip()
            i += 1
        result.append(line)
        i += 1
    return '\n'.join(result)


def _replace_inline_math(md: str) -> str:
    """그리스 문자·유니코드 첨자 → LaTeX inline."""
    lines = md.split('\n')
    in_block = False
    result = []
    for line in lines:
        if line.strip() == '$$':
            in_block = not in_block
            result.append(line)
            continue
        if in_block:
            result.append(line)
            continue

        # 이탤릭으로 감싼 그리스 문자: _ν_ → $\nu$
        for uni, latex in _GREEK_TO_LATEX.items():
            repl = '$' + latex + '$'
            line = line.replace('_' + uni + '_', repl)

        # 유니코드 첨자가 포함된 텍스트 → LaTeX
        # 예: α₀ → $\alpha_0$, E₁₂ → $E_{12}$
        def _replace_sub_sup(m):
            word = m.group(0)
            # 순수 아스키 앞글자 + 첨자
            base = re.sub(r'[₀-₉ₐₑₒₓ⁰-⁹⁺⁻]+', '', word)
            subs = re.findall(r'[₀-₉ₐₑₒₓ]+', word)
            sups = re.findall(r'[⁰-⁹⁺⁻]+', word)
            if not (subs or sups):
                return word
            tex = base
            if subs:
                sub_str = ''.join(s.translate(_UNICODE_SUB) for s in subs)
                tex += f'_{{{sub_str}}}' if len(sub_str) > 1 else f'_{sub_str}'
            if sups:
                sup_str = ''.join(s.translate(_UNICODE_SUP) for s in sups)
                tex += f'^{{{sup_str}}}' if len(sup_str) > 1 else f'^{sup_str}'
            return f'${tex}$'

        if re.search(r'[₀-₉ₐₑₒₓ⁰-⁹⁺⁻]', line):
            parts = re.split(r'(\$[^$\n]+\$)', line)
            new_parts = []
            for part in parts:
                if part.startswith('$') and part.endswith('$'):
                    new_parts.append(part)
                else:
                    new_parts.append(re.sub(r'[A-Za-zα-ωΑ-Ω]*[₀-₉ₐₑₒₓ⁰-⁹⁺⁻]+', _replace_sub_sup, part))
            line = ''.join(new_parts)

        # 독립 그리스 문자 → $\latex$
        for uni, latex in _GREEK_TO_LATEX.items():
            if uni in line:
                repl = '$' + latex + '$'
                parts = re.split(r'(\$[^$\n]+\$)', line)
                new_parts = []
                for part in parts:
                    if part.startswith('$') and part.endswith('$'):
                        new_parts.append(part)
                    else:
                        new_parts.append(part.replace(uni, repl))
                line = ''.join(new_parts)
        result.append(line)
    return '\n'.join(result)


def _fix_latex_blocks(md: str) -> str:
    """LaTeX 블록 cleanup + 위첨자 복원."""
    md = re.sub(r'[$]([^$\n]+\\tag\{\d+\})[$]',
                lambda m: f'$$\n{m.group(1)}\n$$', md)
    _SUP_MAP = {'0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
                '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹'}

    def _restore_sup(m):
        return '⁻' + ''.join(_SUP_MAP.get(d, d) for d in m.group(1))

    md = re.sub(r'\[−\]\[(\d+)\]', _restore_sup, md)
    md = re.sub(r'\[–\]\[(\d+)\]', _restore_sup, md)
    md = re.sub(r'([$][^$\n]+[$])([a-zA-Z]{2,})', r'\1 \2', md)
    return md


def _normalize_reference_numbers(text: str) -> str:
    """구형 PDF 참고문헌 번호 형식 정규화.

    처리 패턴:
    - "- w $x_{N}$ Author" → "- [N] Author"   (Elsevier 구형 인코딩, e.g. Park 2000)
    - "- w x . N Author"   → "- [N] Author"   (동상, 수식 없는 변형)
    - "- $^{N}\\mathrm{X}.$ rest" → "- N. X. rest"  (어깨번호 수식, e.g. Dawant 2022)
    """
    ref_pat = re.compile(
        r'^#{1,3}\s*(?:REFERENCES|References|Bibliography)\b', re.MULTILINE
    )
    m = ref_pat.search(text)
    if not m:
        return text
    pre = text[:m.start()]
    ref = text[m.start():]

    # 1. "- w $x_{N}$" 또는 "- w $x_N$" → "- [N] "
    ref = re.sub(
        r'^- w\s+\$x_\{?(\d+)\}?\$\s*',
        r'- [\1] ',
        ref, flags=re.MULTILINE
    )

    # 2. "- w x . N " (수식 없는 구형 인코딩) → "- [N] "
    ref = re.sub(
        r'^- w x\s*\.\s*(\d+)\s+',
        r'- [\1] ',
        ref, flags=re.MULTILINE
    )

    # 3. "- $^{N}\mathrm{X}.$" → "- N. X." (어깨번호 수식, 공백 포함 변형 대응)
    #    예: "- $^ { 10 } \mathrm { L } .$"  또는  "- ${ }^ { 22 } \mathrm { R } .$"
    def _fix_sup_num(m):
        full = m.group(0)
        nums = re.search(r'\^\s*\{?\s*(\d+)\s*\}?', full)
        letter = re.search(r'\\mathrm\s*\{\s*([A-Z])\s*\}', full)
        if nums and letter:
            return f'- {nums.group(1)}. {letter.group(1)}. '
        return full

    ref = re.sub(
        r'^- \$[^$]*\\mathrm\s*\{\s*[A-Z]\s*\}\s*\.\$\s*',
        _fix_sup_num,
        ref, flags=re.MULTILINE
    )

    # 4. "- N Author" (점 없는 번호) → "- N. Author"
    ref = re.sub(
        r'^- (\d+) ([A-Z])',
        r'- \1. \2',
        ref, flags=re.MULTILINE
    )

    return pre + ref


def _consolidate_references(text: str) -> str:
    """분리된 참조 번호와 내용 합치기 (mineru/marker에서 발생)."""
    ref_start_pat = re.compile(
        r'^\#{1,3}\s*(?:REFERENCES|References|Bibliography)\b', re.MULTILINE
    )
    m = ref_start_pat.search(text)
    if not m:
        return text
    pre_text = text[:m.start()]
    ref_text = text[m.start():]
    ref_num_only = re.compile(r'^\d+\.\s*$')
    next_ref_or_section = re.compile(r'^\d+\.\s+\S|^\#{1,3}\s')
    lines = ref_text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if ref_num_only.match(stripped):
            ref_num = stripped
            i += 1
            fragments = []
            while i < len(lines):
                s = lines[i].strip()
                if next_ref_or_section.match(s):
                    break
                if s:
                    fragments.append(s)
                i += 1
            if fragments:
                result.append(ref_num + ' ' + ' '.join(fragments))
            else:
                result.append(line)
            result.append('')
        else:
            result.append(line)
            i += 1
    return pre_text + '\n'.join(result)


def _clean_journal_boilerplate(text: str) -> str:
    """References 뒤에 나타나는 저널 boilerplate 제거."""
    ref_start_pat = re.compile(
        r'^(?:#{1,3}\s*\*{0,2}\s*|\*{1,3})\s*(?:REFERENCES|References|Bibliography)\b', re.MULTILINE
    )
    m = ref_start_pat.search(text)
    if not m:
        return text
    pre_text = text[:m.start()]
    ref_text = text[m.start():]
    _TRUNCATE = [
        re.compile(r'^#{2,3}\s*\*{0,2}\s*View\s+the\s+article\s+online', re.IGNORECASE),
        re.compile(r'^\*+View\s+the\s+article\s+online', re.IGNORECASE),
        re.compile(r'^#{2,3}\s*\*{0,2}\s*Permissions\s*$', re.IGNORECASE),
        re.compile(r'^#\s+ScienceAdvances\s*$'),
        re.compile(r'^(?:\[)?Use\s+of\s+this\s+article\s+is\s+subject\s+to'),
        re.compile(r'^Science\s+Advances\s+\(ISSN'),
        re.compile(r'^_Science\s+Advances_.*\(ISSN', re.IGNORECASE),
    ]
    _REMOVE = [
        re.compile(r'^##\s+SCIENCE\s+ADVANCES\s+R\s+E'),
        re.compile(r'^SCIENCE\s+ADVANCES\s+\|'),
        re.compile(r'^#\s+Article\s*$'),
        re.compile(r'^#\s+Downloaded\s+from\b'),
        re.compile(r'^\|\s*$'),
        re.compile(r'^Submitted\s+\d'),
        re.compile(r'^Accepted\s+\d'),
        re.compile(r'^Published\s+\d'),
        re.compile(r'^10\.\d{4,5}/\S+\s*$'),
        re.compile(r'^(?:\*+)?Citation:(?:\*+)?\s+[A-Z]'),
        re.compile(r'^Peer\s+review\s+information\b'),
        re.compile(r'^Reprints\s+and\s+permissions\b'),
        re.compile(r"^Publisher.s\s+note\b"),
        re.compile(r'^Springer\s+Nature\s+or\s+its\s+licensor'),
        re.compile(r'^(?:[-*]\s*)?©\s*The\s+Author'),
    ]
    result_lines = []
    for line in ref_text.split('\n'):
        stripped = line.strip()
        if any(pat.match(stripped) for pat in _TRUNCATE):
            break
        if any(pat.match(stripped) for pat in _REMOVE):
            continue
        result_lines.append(line)
    return pre_text + '\n'.join(result_lines)


def _inject_figure_captions(md: str) -> str:
    """Figure 캡션을 이미지 바로 뒤 blockquote(>)로 이동."""
    lines = md.split('\n')
    FIG_START = re.compile(
        r'^\s*(?:\*\*)?(?:Figure|Fig\.)\s+(\d+)[.\|]', re.IGNORECASE
    )
    # ". . . Fig. N." 또는 ". . Fig. N." 형태 (구형 PDF Docling 출력)
    DOT_FIG_START = re.compile(
        r'^\.\s*\.\s*\.?\s*(?:Fig\.?|Figure)\s+(\d+)[.\|]', re.IGNORECASE
    )
    captions: dict[int, str] = {}
    cap_line_ranges: list[tuple[int, int]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        # ". . . Fig. N." 패턴 → FIG_START처럼 처리 (줄 앞의 점들 제거)
        dot_m = DOT_FIG_START.match(line.strip())
        if dot_m:
            line = re.sub(r'^\.\s*\.\s*\.?\s*', '', line.strip())
            lines[i] = line
        m = FIG_START.match(line)
        if m:
            fig_num = int(m.group(1))
            if line.strip().startswith('>'):
                i += 1
                continue
            cap_lines = [line.strip()]
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt:
                    break
                if nxt.startswith('#') or nxt.startswith('![') or nxt.startswith('>'):
                    break
                if FIG_START.match(nxt):
                    break
                cap_lines.append(nxt)
                j += 1
            cap_text = ' '.join(cap_lines)
            cap_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', cap_text)
            cap_text = re.sub(r'\*\*', '', cap_text).strip()
            captions[fig_num] = cap_text
            cap_line_ranges.append((i, j - 1))
            i = j
        else:
            i += 1

    if not captions:
        return md

    remove_lines = set()
    for start, end in cap_line_ranges:
        for k in range(start, end + 1):
            remove_lines.add(k)

    new_lines = []
    for i, line in enumerate(lines):
        if i in remove_lines:
            if not new_lines or new_lines[-1] != '':
                new_lines.append('')
            continue
        new_lines.append(line)

    cap_queue = list(sorted(captions.items()))
    cap_idx = 0
    output = []
    for i, line in enumerate(new_lines):
        output.append(line)
        if line.strip().startswith('!['):
            j = i + 1
            while j < len(new_lines) and not new_lines[j].strip():
                j += 1
            if j < len(new_lines) and new_lines[j].strip().startswith('> '):
                continue  # 이미 blockquote 캡션 있음
            if j < len(new_lines) and new_lines[j].strip().startswith('*Figure'):
                continue  # 이미 italic 캡션 있음 (Docling hybrid)
            if j < len(new_lines) and new_lines[j].strip().startswith('*Fig.'):
                continue  # 동상
            if j < len(new_lines) and FIG_START.match(new_lines[j].strip()):
                continue
            if cap_idx < len(cap_queue):
                fn, cap_text = cap_queue[cap_idx]
                output.append('')
                output.append(f'> {cap_text}')
                cap_idx += 1

    final = []
    for line in output:
        if FIG_START.match(line.strip()) and not line.strip().startswith('>'):
            clean = re.sub(r'\*\*([^*]+)\*\*', r'\1', line.strip())
            clean = re.sub(r'\*\*', '', clean).strip()
            final.append(f'> {clean}')
        else:
            final.append(line)

    return '\n'.join(final)


def _apply_journal_paper(md: str) -> str:
    """Layer 2: journal_paper 문서 유형 규칙 적용."""
    md = _convert_bold_headings(md)
    md = _normalize_headings(md)
    md = _format_abstract(md)
    md = _merge_column_linebreaks(md)
    md = _replace_inline_math(md)
    md = _fix_latex_blocks(md)
    md = _normalize_reference_numbers(md)
    md = _consolidate_references(md)
    md = _clean_journal_boilerplate(md)
    md = _inject_figure_captions(md)
    return md


def _apply_journal_paper_hybrid(md: str) -> str:
    """Layer 2: journal_paper rules for Hybrid pipeline."""
    md = _convert_bold_headings(md)
    md = _normalize_headings(md)
    md = _format_abstract(md)
    md = _merge_column_linebreaks(md)
    md = _replace_inline_math(md)
    md = _fix_latex_blocks(md)
    md = _normalize_reference_numbers(md)
    md = _consolidate_references(md)
    md = _clean_journal_boilerplate(md)
    # 이미 *Figure / > 캡션이 있는 이미지는 건드리지 않음 (_inject_figure_captions 내 체크)
    md = _inject_figure_captions(md)
    return md


# ═══════════════════════════════════════════════════════════════════
# LAYER 3 — ENGINE QUIRKS
# ═══════════════════════════════════════════════════════════════════

_MARKER_KEY_SECTION_H1 = re.compile(
    r'^# ((?:Abstract|Introduction|Results?(?:\s+and\s+Discussion)?|'
    r'Discussion|Conclusions?|Experimental(?:\s+Section)?|Methods?|'
    r'Materials(?:\s+and\s+Methods)?|Summary|Acknowledgem\w+|'
    r'Supporting\s+Information)\s*$)',
    re.IGNORECASE | re.MULTILINE,
)


def _apply_marker_quirks(md: str) -> str:
    """Marker 엔진 출력 특수 수정."""
    # Template Picture 제거 (_page_X_Picture_X.jpeg)
    md = re.sub(r'!\[\]\(_page_\d+_Picture_\d+\.jpeg\)\n?', '', md)
    # References 형식: - [N] → [N]
    md = re.sub(r'^- \[(\d+)\] ', r'[\1] ', md, flags=re.MULTILINE)
    # 앵커 링크가 포함된 연도 표기 정규화: [\(2019\)](#page-X-Y) → (2019)
    md = re.sub(r'\[\\?\((\d{4}[^)]*?)\\?\)\]\(#page-[\d\-]+\)', r'(\1)', md)
    # 잔여 이스케이프 괄호: \(2019\) → (2019)
    md = re.sub(r'\\?\((\d{4})\)\.?', r'(\1)', md)
    # 핵심 섹션 h1 헤딩을 h2로 승격 (구조 점수 개선: # Conclusions → ## Conclusions)
    md = _MARKER_KEY_SECTION_H1.sub(r'## \1', md)
    return md


_NUMBERED_SECTION = re.compile(
    r'^(\d+\.\s+(?:Introduction|Results?(?:\s+and\s+Discussion)?|Discussion|'
    r'Conclusions?|Experimental(?:\s+Section)?|Methods?|'
    r'Materials(?:\s+and\s+Methods)?|Summary|Acknowledgem\w+|'
    r'Supporting\s+Information|Supplementary).*)',
    re.IGNORECASE,
)


_PYMUPDF4LLM_IMG = re.compile(
    r'!\[([^\]]*)\]\(([^)]+\.pdf-(\d+)-\d+\.[a-zA-Z]+)\)'
)


def _dedup_pymupdf_page_images(md: str, max_per_page: int = 2) -> str:
    """동일 PDF 페이지 내 과잉 서브패널 이미지 제거 (페이지당 최대 max_per_page개 유지).

    PyMuPDF4LLM은 논문의 하위 패널(a/b/c/d)을 개별 이미지로 추출하여 이미지 수가
    Ground Truth보다 2~5배 많아지는 문제가 발생. 같은 페이지의 이미지가 max_per_page
    초과 시 초과분을 제거해 Figure 수 정확도를 높인다.
    """
    from collections import defaultdict
    page_counts: dict[str, int] = defaultdict(int)

    def _filter(m: re.Match) -> str:
        page_num = m.group(3)
        page_counts[page_num] += 1
        if page_counts[page_num] > max_per_page:
            return ''   # 제거
        return m.group(0)  # 유지

    # 각 이미지 참조를 순서대로 처리 (첫 max_per_page개 유지)
    result = _PYMUPDF4LLM_IMG.sub(_filter, md)
    # 이미지 제거 후 남은 빈 줄 정리
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


def _apply_pymupdf4llm_quirks(md: str) -> str:
    """PyMuPDF4LLM 엔진 출력 특수 수정."""
    # 숫자형 섹션 헤딩 변환: "1. Introduction" → "## 1. Introduction"
    # Wiley(Small Structures) 등 번호 붙은 섹션 헤딩이 plain text로 출력됨
    lines = md.split('\n')
    result = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # 빈 줄 앞뒤에 위치한 독립 숫자 섹션 줄만 변환
        prev_empty = (i == 0 or lines[i-1].strip() == '')
        next_empty = (i == len(lines)-1 or lines[i+1].strip() == '')
        if prev_empty and next_empty and _NUMBERED_SECTION.match(stripped):
            result.append(f'## {stripped}')
        else:
            result.append(line)
    md = '\n'.join(result)
    # 페이지당 이미지 개수 제한 (서브패널 중복 제거)
    md = _dedup_pymupdf_page_images(md, max_per_page=2)
    return md


_DOCLING_LIGATURES = {
    '/uniFB00': 'ff', '/uniFB01': 'fi', '/uniFB02': 'fl',
    '/uniFB03': 'ffi', '/uniFB04': 'ffl',
}
_DOCLING_ESCAPES = {'/C0 ': '-', '/C0': '-'}


def _apply_docling_quirks(md: str) -> str:
    """Docling 엔진 출력 특수 수정: /uXXXX → Unicode, /uniFBxx → 리가처."""
    # 리가처 변환 (/uniFBxx는 5자리 /uXXXX 치환 전에 처리)
    for lig, rep in _DOCLING_LIGATURES.items():
        md = md.replace(lig, rep)
    # /C0 하이픈
    for esc, rep in _DOCLING_ESCAPES.items():
        md = md.replace(esc, rep)
    # /uXXXXX (5자리) → Unicode character (5자리 먼저)
    def _u_replace(m: re.Match) -> str:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)
    md = re.sub(r'/u([0-9A-Fa-f]{5})', _u_replace, md)
    # /uXXXX (4자리) → Unicode character
    md = re.sub(r'/u([0-9A-Fa-f]{4})', _u_replace, md)
    return md


def _apply_mineru_quirks(md: str) -> str:
    """MinerU 엔진 출력 특수 수정."""
    return md


def _apply_engine_quirks(md: str, engine: str | None) -> str:
    """Layer 3: 엔진별 특수 수정."""
    if engine == 'marker':
        md = _apply_marker_quirks(md)
    elif engine == 'pymupdf4llm':
        md = _apply_pymupdf4llm_quirks(md)
    elif engine == 'docling':
        md = _apply_docling_quirks(md)
    elif engine == 'mineru':
        md = _apply_mineru_quirks(md)
    return md


# ═══════════════════════════════════════════════════════════════════
# MAIN API
# ═══════════════════════════════════════════════════════════════════

def postprocess(md_text: str,
                engine: str | None = None,
                doc_type: str = 'journal_paper') -> str:
    """통합 후처리 파이프라인.

    Args:
        md_text:  입력 Markdown 텍스트
        engine:   엔진 이름 ('docling' | 'marker' | 'mineru' | 'pymupdf4llm' | 'hybrid')
                  'hybrid': Docling+UniMerNet 하이브리드 파이프라인용
                    - _inject_figure_captions() 생략 (Docling이 이미 배치)
                    - Layer 3 engine quirks 생략 (normalize_docling_md에서 이미 적용)
        doc_type: 문서 유형 ('journal_paper' | 'report' | 'admin_doc')

    Returns:
        후처리된 Markdown 텍스트
    """
    # Layer 1: Universal
    md_text = _apply_universal(md_text)
    # Layer 2: Doc type
    if doc_type == 'journal_paper':
        if engine == 'hybrid':
            md_text = _apply_journal_paper_hybrid(md_text)
        else:
            md_text = _apply_journal_paper(md_text)
    # Layer 3: Engine quirks (hybrid는 이미 normalize_docling_md에서 적용됨)
    if engine != 'hybrid':
        md_text = _apply_engine_quirks(md_text, engine)
    # Final cleanup
    md_text = _fix_blank_lines(md_text)
    return md_text
