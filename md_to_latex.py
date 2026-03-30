#!/usr/bin/env python3
"""
MD → LaTeX 변환기 (hybrid_v8 출력 전용)

hybrid_v8이 생성한 _Hybrid_Full.md를 MikTeX 컴파일 가능한 .tex + .bib으로 변환.

특징:
  - YAML frontmatter에서 메타데이터 자동 추출 (제목/저자/소속/키워드)
  - $...$ / $$...$$ → equation/equation* 환경 변환
  - MD 표 → tabular 환경 (+ PNG 그림 fallback)
  - ![...](path) → \includegraphics
  - 참고문헌 섹션 → \bibitem 목록 + references.bib 생성
  - 저널별 documentclass 자동 선택 (journal_profiles.json 참조)

실행:
    python scripts/md_to_latex.py <md_file> [--journal wiley|nature|acs|elsevier|generic]
    python scripts/md_to_latex.py <md_file> --out-dir <output_dir>

출력:
    <out_dir>/
      submission.tex
      references.bib
      figures/        (이미지 복사)
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

VAULT_ROOT  = Path(__file__).parent.parent
SCRIPTS_DIR = Path(__file__).parent


# ─────────────────────────────────────────────────────────────────────────────
# YAML frontmatter 파싱
# ─────────────────────────────────────────────────────────────────────────────

def parse_yaml_frontmatter(md: str) -> tuple[dict, str]:
    """YAML frontmatter 파싱. (meta_dict, body) 반환."""
    meta: dict = {}
    if not md.startswith('---'):
        return meta, md
    end = md.find('\n---', 3)
    if end < 0:
        return meta, md
    yaml_block = md[3:end].strip()
    body = md[end + 4:].lstrip('\n')

    lines = yaml_block.split('\n')
    in_list_key: str = ''
    list_items: list = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue

        # 리스트 항목 (- value)
        if stripped.startswith('- ') and in_list_key:
            list_items.append(stripped[2:].strip().strip('"').strip("'"))
            continue

        # key: value 또는 key:[] 또는 key:
        if ':' in stripped and not stripped.startswith('-'):
            # 이전 리스트 저장
            if in_list_key:
                meta[in_list_key] = list_items
                in_list_key = ''
                list_items = []

            key, _, val = stripped.partition(':')
            key = key.strip()
            val = val.strip()

            # affiliations:[] 처럼 인라인 빈 리스트
            if val in ('[]', '[ ]', ''):
                in_list_key = key
                list_items = []
                meta[key] = []  # 빈 리스트 기본값
            elif val.startswith('[') and val.endswith(']'):
                # 인라인 리스트: [a, b, c]
                inner = val[1:-1].strip()
                if inner:
                    meta[key] = [x.strip().strip('"').strip("'") for x in inner.split(',')]
                else:
                    meta[key] = []
            else:
                meta[key] = val.strip('"').strip("'")

    # 마지막 리스트 저장
    if in_list_key and list_items:
        meta[in_list_key] = list_items

    return meta, body


# ─────────────────────────────────────────────────────────────────────────────
# 참고문헌 파싱 → BibTeX 생성
# ─────────────────────────────────────────────────────────────────────────────

_KNOWN_JOURNALS = {
    'Nat. Commun.': 'Nature Communications',
    'Nat. Mater.': 'Nature Materials',
    'Nat. Electron.': 'Nature Electronics',
    'Nat. Methods': 'Nature Methods',
    'Adv. Mater.': 'Advanced Materials',
    'Adv. Funct. Mater.': 'Advanced Functional Materials',
    'Adv. Sci.': 'Advanced Science',
    'ACS Nano': 'ACS Nano',
    'Nano Lett.': 'Nano Letters',
    'Nano Energy': 'Nano Energy',
    'npj Flex. Electron.': 'npj Flexible Electronics',
    'Science': 'Science',
    'Science Advances': 'Science Advances',
    'Sci. Adv.': 'Science Advances',
    'Small': 'Small',
    'Small Struct.': 'Small Structures',
    'ACS Appl. Mater. Interfaces': 'ACS Applied Materials & Interfaces',
    'Chem. Mater.': 'Chemistry of Materials',
    'Angew. Chem.': 'Angewandte Chemie',
    'JACS': 'Journal of the American Chemical Society',
    'J. Am. Chem. Soc.': 'Journal of the American Chemical Society',
    'Renew. Sust. Energ. Rev.': 'Renewable and Sustainable Energy Reviews',
}


def _parse_references(ref_block: str) -> list[dict]:
    """참고문헌 텍스트 → 구조화된 리스트.

    지원 형식:
      [1] Author et al. Title. Journal Vol, Pages (Year).
      1. Author et al. ...
      - Author et al. ...
    """
    refs = []
    # 번호 기반 분리
    entries = re.split(r'\n(?=\[\d+\]|\d+\.\s+)', ref_block.strip())

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue

        # 번호 추출
        m_num = re.match(r'^\[?(\d+)\]?\.?\s+', entry)
        if not m_num:
            # "-" 로 시작하는 경우
            m_num = re.match(r'^-\s+', entry)
            num = len(refs) + 1
            text = entry[m_num.end():] if m_num else entry
        else:
            num = int(m_num.group(1))
            text = entry[m_num.end():]

        # DOI 추출
        doi_m = re.search(r'\b(?:https?://doi\.org/|DOI:\s*)(10\.\d{4,}/\S+)', text, re.IGNORECASE)
        doi = doi_m.group(1).rstrip('.,)') if doi_m else ''

        # 연도 추출
        year_m = re.search(r'\b(19|20)\d{2}\b', text)
        year = year_m.group(0) if year_m else ''

        # ── 저자/저널 추출 (Wiley 형식) ──────────────────────────────
        # 형식: "A. B. Author, C. D. Name, ..., Journal Name Year, Vol, Pages."
        # 전략: 연도 직전 쉼표 그룹을 저널명으로, 나머지 앞부분을 저자로
        authors_raw = ''
        journal = ''

        if year:
            before_year = text[:text.find(year)].strip().rstrip(',').rstrip()
            # 쉼표로 분리
            parts = [p.strip() for p in before_year.split(',')]
            # 뒤에서부터 저널명 찾기: 이니셜 패턴이 아닌 첫 번째 그룹
            journal_idx = len(parts)
            for j in range(len(parts) - 1, -1, -1):
                part = parts[j]
                # 이니셜+성 패턴: "X. Name" 또는 "X.-Y. Name" → 저자
                if re.match(r'^[A-Z][\.\-]', part) and re.search(r'[A-Z][a-z]{2,}', part):
                    break
                # et al.
                if re.match(r'^et\s+al', part, re.IGNORECASE):
                    break
                # 저널명 후보: 대문자 시작 + 소문자 포함 (약어)
                if re.search(r'[A-Z][a-z]', part) and len(part) > 2:
                    journal_idx = j
            if journal_idx < len(parts):
                journal = ', '.join(parts[journal_idx:]).strip()
                authors_raw = ', '.join(parts[:journal_idx]).strip().rstrip(',')
            else:
                authors_raw = before_year

            # Validate journal: should not contain digits (volume/page numbers)
            if journal and re.search(r'\d', journal):
                # Probably grabbed volume/page — find last all-letter part
                parts_check = [p.strip() for p in before_year.split(',')]
                for j2 in range(len(parts_check)-1, -1, -1):
                    candidate = parts_check[j2].strip()
                    if not re.search(r'\d', candidate) and len(candidate) > 2:
                        journal = candidate
                        authors_raw = ', '.join(parts_check[:j2]).strip().rstrip(',')
                        break

            # Try to match known journal abbreviations
            if journal:
                for abbr, full in _KNOWN_JOURNALS.items():
                    if abbr.lower() in journal.lower() or journal.lower() in abbr.lower():
                        journal = abbr  # use standard abbreviation
                        break
        else:
            authors_raw = text[:100]

        # authors_raw 정리: "et al." 이후 제거
        etal_m = re.search(r',?\s*et\s+al\.?', authors_raw, re.IGNORECASE)
        if etal_m:
            authors_raw = authors_raw[:etal_m.start()].rstrip(',').strip() + ' et al.'
        authors_raw = authors_raw.strip().rstrip(',').strip()

        # 제목 추출 (따옴표나 이탤릭 내부)
        title_m = re.search(r'["""](.*?)["""]', text)
        if not title_m:
            title_m = re.search(r'\*([^*]+)\*', text)
        title = title_m.group(1).strip() if title_m else ''

        # BibTeX 키 생성: 1저자성 + 연도
        # "H. W. Choi, ..." → "Choi2024"
        # 이름 토큰에서 마지막 대문자 시작 긴 단어 = 성
        author_tokens = re.split(r'[\s,]+', authors_raw)
        last_name = ''
        for tok in reversed(author_tokens):
            tok_clean = tok.strip('.,')
            if tok_clean and tok_clean[0].isupper() and len(tok_clean) > 2:
                last_name = tok_clean
                break
        if not last_name and author_tokens:
            last_name = author_tokens[0]

        bib_key = f"{last_name}{year}" if year else f"ref{num}"
        bib_key = re.sub(r'[^a-zA-Z0-9]', '', bib_key)

        refs.append({
            'num': num,
            'key': bib_key or f'ref{num}',
            'authors': authors_raw,
            'title': title,
            'journal': journal,
            'year': year,
            'doi': doi,
            'raw': text.strip(),
        })

    return refs


def _refs_to_bibtex(refs: list[dict]) -> str:
    """참고문헌 리스트 → BibTeX 문자열."""
    lines = ['% Auto-generated by md_to_latex.py (hybrid_v8)', '']
    seen_keys: dict[str, int] = {}

    for r in refs:
        key = r['key']
        # 중복 키 처리
        if key in seen_keys:
            seen_keys[key] += 1
            key = f"{key}{chr(96 + seen_keys[key])}"  # a, b, c...
        else:
            seen_keys[key] = 1

        r['_bib_key'] = key  # 후속 \cite{} 사용을 위해 업데이트

        lines.append(f'@article{{{key},')
        if r['authors']:
            lines.append(f'  author  = {{{r["authors"]}}},')

        # title: 명시된 것 없으면 저자+저널+연도로 구성 (Zotero 표시용)
        title = r['title']
        if not title:
            parts = []
            if r['authors']:
                first_author = r['authors'].split(',')[0].strip()
                parts.append(first_author)
            if r['journal']:
                parts.append(r['journal'])
            if r['year']:
                parts.append(r['year'])
            title = ' — '.join(parts) if parts else f'[{r["num"]}]'
        lines.append(f'  title   = {{{{{title}}}}},')

        if r['journal']:
            lines.append(f'  journal = {{{r["journal"]}}},')
        if r['year']:
            lines.append(f'  year    = {{{r["year"]}}},')
        if r['doi']:
            lines.append(f'  doi     = {{{r["doi"]}}},')
        note_raw = r["raw"][:120].replace("{","(").replace("}",")")
        lines.append(f'  note    = {{[{r["num"]}] {note_raw}...}},')
        lines.append('}')
        lines.append('')

    return '\n'.join(lines)


def _refs_to_bibitem(refs: list[dict]) -> str:
    """참고문헌 → \\bibitem 목록 (thebibliography 환경용)."""
    lines = []
    for r in refs:
        key = r.get('_bib_key', r['key'])
        raw_escaped = _escape_latex(r['raw'])
        lines.append(f'\\bibitem{{{key}}}')
        lines.append(f'  {raw_escaped}')
        lines.append('')
    return '\n'.join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# LaTeX 이스케이프
# ─────────────────────────────────────────────────────────────────────────────

def _escape_latex(text: str) -> str:
    """일반 텍스트 → LaTeX 이스케이프 (수식 내부 제외)."""
    # 유니코드 수학/특수문자 먼저 치환 (✉ 등이 이스케이프되기 전에 처리)
    for uni, latex in _UNICODE_MATH_MAP:
        text = text.replace(uni, latex)
    # 이미 LaTeX 명령이면 특수문자 이스케이프 생략
    if '\\' in text:
        return text
    replacements = [
        ('&', r'\&'), ('%', r'\%'), ('$', r'\$'), ('#', r'\#'),
        ('_', r'\_'), ('{', r'\{'), ('}', r'\}'), ('~', r'\textasciitilde{}'),
        ('^', r'\textasciicircum{}'),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


# 유니코드 수학/그리스 문자 → LaTeX 매핑
_UNICODE_MATH_MAP: list[tuple[str, str]] = [
    # 볼드 이탤릭 그리스 문자 (Mathematical Bold Italic, U+1D700 범위)
    ('𝝂', r'$\boldsymbol{\nu}$'),     # U+1D742
    ('𝝈', r'$\boldsymbol{\sigma}$'),   # U+1D748
    ('𝜺', r'$\boldsymbol{\varepsilon}$'),  # U+1D73A
    ('𝜶', r'$\boldsymbol{\alpha}$'),   # U+1D736
    ('𝜷', r'$\boldsymbol{\beta}$'),    # U+1D737
    ('𝜸', r'$\boldsymbol{\gamma}$'),   # U+1D738
    ('𝜽', r'$\boldsymbol{\theta}$'),   # U+1D73D
    # Italic 그리스 문자 (Mathematical Italic, U+1D6xx/U+1D7xx)
    ('𝛾', r'$\gamma$'),    # U+1D6FE italic gamma
    ('𝜎', r'$\sigma$'),    # U+1D70E italic sigma
    ('𝜀', r'$\varepsilon$'),  # U+1D700 italic epsilon
    ('𝜈', r'$\nu$'),        # U+1D708 italic nu
    ('𝜃', r'$\theta$'),     # U+1D703 italic theta
    ('𝜏', r'$\tau$'),       # U+1D70F italic tau
    ('𝜇', r'$\mu$'),        # U+1D707 italic mu
    # 일반 그리스 문자 (텍스트에 나타날 때)
    ('Α', r'$A$'), ('Β', r'$B$'),
    ('Γ', r'$\Gamma$'), ('Δ', r'$\Delta$'), ('Ε', r'$E$'),
    ('Ζ', r'$Z$'), ('Η', r'$H$'), ('Θ', r'$\Theta$'),
    ('Κ', r'$K$'), ('Λ', r'$\Lambda$'), ('Μ', r'$M$'),
    ('Ν', r'$N$'), ('Ξ', r'$\Xi$'), ('Ο', r'$O$'),
    ('Π', r'$\Pi$'), ('Ρ', r'$P$'), ('Σ', r'$\Sigma$'),
    ('Τ', r'$T$'), ('Υ', r'$\Upsilon$'), ('Φ', r'$\Phi$'),
    ('Χ', r'$X$'), ('Ψ', r'$\Psi$'), ('Ω', r'$\Omega$'),
    ('α', r'$\alpha$'), ('β', r'$\beta$'), ('γ', r'$\gamma$'),
    ('δ', r'$\delta$'), ('ε', r'$\varepsilon$'), ('ζ', r'$\zeta$'),
    ('η', r'$\eta$'), ('θ', r'$\theta$'), ('λ', r'$\lambda$'),
    ('μ', r'$\mu$'), ('ν', r'$\nu$'), ('π', r'$\pi$'),
    ('ρ', r'$\rho$'), ('σ', r'$\sigma$'), ('τ', r'$\tau$'),
    ('φ', r'$\varphi$'), ('ϕ', r'$\phi$'), ('χ', r'$\chi$'),
    ('ψ', r'$\psi$'), ('ω', r'$\omega$'),
    # 수학 기호
    ('→', r'$\rightarrow$'), ('←', r'$\leftarrow$'),
    ('↑', r'$\uparrow$'), ('↓', r'$\downarrow$'),
    ('⇒', r'$\Rightarrow$'), ('⇔', r'$\Leftrightarrow$'),
    ('≈', r'$\approx$'), ('≤', r'$\leq$'), ('≥', r'$\geq$'),
    ('≠', r'$\neq$'), ('≡', r'$\equiv$'),
    ('∼', r'$\sim$'),   # U+223C tilde operator
    ('∈', r'$\in$'),    # U+2208 element of
    ('∉', r'$\notin$'), ('∋', r'$\ni$'),
    ('⊂', r'$\subset$'), ('⊃', r'$\supset$'),
    ('∩', r'$\cap$'), ('∪', r'$\cup$'),
    ('∑', r'$\sum$'), ('∏', r'$\prod$'), ('∫', r'$\int$'),
    ('×', r'$\times$'), ('÷', r'$\div$'), ('±', r'$\pm$'),
    ('∞', r'$\infty$'), ('∂', r'$\partial$'), ('∇', r'$\nabla$'),
    ('√', r'$\sqrt{}$'),
    ('−', r'$-$'),      # U+2212 minus sign (텍스트 모드에서 하이픈과 다름)
    ('′', r"$'$"),      # U+2032 prime
    ('″', r"$''$"),     # U+2033 double prime
    ('°', r'$^{\circ}$'),
    # 특수 기호 (텍스트 모드)
    ('✉', r'\texttt{[email]}'),  # U+2709 envelope → 이메일 표시
    # 구두점/타이포그래피
    ('─', r'--'), ('–', r'--'), ('—', r'---'),
    (''', r"'"), (''', r"'"), ('"', r'``'), ('"', r"''"),
    ('…', r'\ldots{}'),
]


def _replace_unicode_math(text: str) -> str:
    """유니코드 수학/그리스 문자를 LaTeX 명령으로 치환 (수식 외부)."""
    # 수식 구간 스킵: $...$ 안은 그대로
    result = []
    i = 0
    while i < len(text):
        # $...$ 구간 시작 감지
        if text[i] == '$':
            # $$ 블록
            if text[i:i+2] == '$$':
                end = text.find('$$', i + 2)
                if end >= 0:
                    result.append(text[i:end + 2])
                    i = end + 2
                    continue
            # $ 블록
            end = text.find('$', i + 1)
            if end >= 0:
                result.append(text[i:end + 1])
                i = end + 1
                continue
        result.append(text[i])
        i += 1

    joined = ''.join(result)
    for uni, latex in _UNICODE_MATH_MAP:
        joined = joined.replace(uni, latex)
    return joined


def _fix_latex_math_spaces(content: str) -> str:
    """수식 내부 텍스트에서 과도한 공백 정규화 (중괄호 내부 spaced chars 등).

    hybrid_v8 UniMerNet 출력 패턴:
      \\mathrm { a x i a l }  →  \\mathrm{axial}
      E _ { x }  →  E_{x}
      3 . 5  →  3.5 (숫자 사이 공백)
    """
    # 중괄호 내부 공백-분리 단일 글자들 합치기: {a x i a l} → {axial}
    def _collapse_braces(m):
        inner = m.group(1)
        if re.match(r'^[a-zA-Z0-9](?:\s+[a-zA-Z0-9])+$', inner.strip()):
            collapsed = re.sub(r'\s+', '', inner.strip())
            return '{' + collapsed + '}'
        return m.group(0)
    # 반복 적용 (중첩 중괄호: \mathrm { \mathrm { x } } )
    for _ in range(3):
        content = re.sub(r'\{([^{}]+)\}', _collapse_braces, content)
    # _ { x } → _{x}, ^ { x } → ^{x}
    content = re.sub(r'_\s*\{\s*([^{}]+?)\s*\}', lambda m: '_{' + m.group(1).strip() + '}', content)
    content = re.sub(r'\^\s*\{\s*([^{}]+?)\s*\}', lambda m: '^{' + m.group(1).strip() + '}', content)
    # 숫자 . 숫자 사이 공백: 3 . 5 → 3.5
    content = re.sub(r'(\d)\s+\.\s+(\d)', r'\1.\2', content)
    return content


def _normalize_latex_spaces(text: str) -> str:
    """$...$ / $$...$$ 블록 내부에 _fix_latex_math_spaces 적용."""
    result_parts = []
    i = 0
    while i < len(text):
        if text[i:i+2] == '$$':
            end = text.find('$$', i + 2)
            if end >= 0:
                result_parts.append('$$' + _fix_latex_math_spaces(text[i+2:end]) + '$$')
                i = end + 2
                continue
        elif text[i] == '$':
            end = text.find('$', i + 1)
            if end >= 0 and '\n' not in text[i+1:end]:
                result_parts.append('$' + _fix_latex_math_spaces(text[i+1:end]) + '$')
                i = end + 1
                continue
        result_parts.append(text[i])
        i += 1
    return ''.join(result_parts)


def _escape_latex_safe(text: str) -> str:
    """수식 마커를 보호하면서 이스케이프."""
    # 유니코드 수학 문자 먼저 변환
    text = _replace_unicode_math(text)

    # 수식 토큰을 임시 플레이스홀더로 교체
    formulas = []
    def _stash(m):
        formulas.append(m.group(0))
        return f'FMLPLACEHOLDER{len(formulas)-1}END'

    text = re.sub(r'\$\$[\s\S]+?\$\$', _stash, text)
    text = re.sub(r'(?<!\$)\$(?!\$)[^$\n]+(?<!\$)\$(?!\$)', _stash, text)

    # 이스케이프
    for old, new in [('&', r'\&'), ('%', r'\%'), ('#', r'\#'),
                     ('~', r'\textasciitilde{}'), ('^', r'\textasciicircum{}')]:
        text = text.replace(old, new)

    # 복원
    for i, f in enumerate(formulas):
        text = text.replace(f'FMLPLACEHOLDER{i}END', f)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# MD 표 → LaTeX tabular
# ─────────────────────────────────────────────────────────────────────────────

def _md_table_to_latex(table_lines: list[str], table_num: int,
                       figures_dir: Path, assets_dir: Path | None) -> tuple[str, str | None]:
    """MD 표를 LaTeX tabular로 변환. 복잡한 표는 PNG fallback.

    Returns:
        (latex_str, png_path_or_None)
    """
    rows = []
    for line in table_lines:
        if not line.strip():
            continue
        # 구분선 무시 (|---|---|)
        if re.match(r'^\|[\s\-:|]+\|$', line.strip()):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        rows.append(cells)

    if not rows:
        return '', None

    n_cols = max(len(r) for r in rows)

    # 복잡도 판정: 셀 내에 \n이나 merged cell 패턴이 있으면 PNG fallback 권장
    # 하지만 실제 PNG 생성은 외부 도구 필요 → 주석으로 표시
    has_complex = any(
        len(r) != n_cols or any(len(c) > 100 for c in r)
        for r in rows
    )

    col_spec = 'l' + 'c' * (n_cols - 1) if n_cols > 1 else 'l'

    lines = [
        f'\\begin{{table}}[htbp]',
        f'  \\centering',
        f'  \\caption{{Table {table_num}}}',
        f'  \\label{{tab:table{table_num}}}',
        f'  \\begin{{tabular}}{{{col_spec}}}',
        f'    \\hline',
    ]

    for i, row in enumerate(rows):
        # 셀 수 맞추기
        while len(row) < n_cols:
            row.append('')
        cells_latex = [_escape_latex_safe(c) for c in row[:n_cols]]
        row_str = ' & '.join(cells_latex) + r' \\'
        lines.append(f'    {row_str}')
        if i == 0:  # 헤더 행 이후 구분선
            lines.append(f'    \\hline')

    lines += [
        f'    \\hline',
        f'  \\end{{tabular}}',
        f'\\end{{table}}',
    ]

    latex_str = '\n'.join(lines)

    # 복잡한 표: tabular + 이미지 fallback 주석 추가
    if has_complex:
        latex_str = (
            f'% [복잡한 표 — tabular 변환 시 레이아웃 확인 필요]\n'
            f'% 원본 이미지가 있다면 figures/ 폴더에 table{table_num}.png 추가 후 아래 주석 해제:\n'
            f'% \\begin{{figure}}[htbp]\\centering\n'
            f'% \\includegraphics[width=\\linewidth]{{figures/table{table_num}.png}}\n'
            f'% \\caption{{Table {table_num}}}\\label{{tab:table{table_num}}}\n'
            f'% \\end{{figure}}\n'
            + latex_str
        )

    return latex_str, None


# ─────────────────────────────────────────────────────────────────────────────
# MD 본문 → LaTeX 본문 변환
# ─────────────────────────────────────────────────────────────────────────────

def _convert_body(body: str, figures_dir: Path, assets_src: Path | None,
                  refs: list[dict]) -> tuple[str, list[str]]:
    """MD 본문 → LaTeX 본문. (latex_body, copied_figures) 반환."""
    figures_dir.mkdir(parents=True, exist_ok=True)
    copied_figures: list[str] = []
    fig_counter = [0]
    table_counter = [0]

    lines = body.split('\n')
    result: list[str] = []
    i = 0

    # 참고문헌 cite 매핑 ([1] → \cite{key})
    cite_map: dict[str, str] = {}
    for r in refs:
        key = r.get('_bib_key', r['key'])
        cite_map[str(r['num'])] = key

    def _fix_inline_math(text: str) -> str:
        """인라인 수식 $...$ 내 문제 있는 LaTeX 명령 수정."""
        def _fix_math_group(m):
            content = m.group(1)
            # \textit{...} → 내용만 (수식 내에서는 이탤릭이 기본)
            content = re.sub(r'\\textit\s*\{', '{', content)
            # \textbf{...} → \mathbf{...}
            content = re.sub(r'\\textbf\s*\{', r'\\mathbf{', content)
            # \AA (텍스트 모드 옹스트롬) → \text{\AA} 또는 제거
            content = re.sub(r'\\AA\b', r'\\text{\\AA}', content)
            # \r 등 텍스트 전용 accent 명령 제거 (OCR 쓰레기)
            content = re.sub(r'\\[`\'^"~=.][A-Za-z]', '', content)
            # 중괄호 균형 맞추기
            depth = 0
            fixed = []
            for ch in content:
                if ch == '{':
                    depth += 1
                    fixed.append(ch)
                elif ch == '}':
                    if depth > 0:
                        depth -= 1
                        fixed.append(ch)
                    # else: 불균형 } 제거
                else:
                    fixed.append(ch)
            # 열린 { 닫기
            fixed_str = ''.join(fixed) + '}' * depth
            return f'${fixed_str}$'
        return re.sub(r'(?<!\$)\$(?!\$)([^$\n]+?)(?<!\$)\$(?!\$)', _fix_math_group, text)

    def _convert_inline(text: str) -> str:
        """인라인 MD → LaTeX 변환."""
        # 수식 임시 보호 (Bold/Italic 패턴이 수식 내 * _ 를 오인하지 않도록)
        math_stash: list[str] = []
        def _math_stash(m):
            math_stash.append(m.group(0))
            return f'MATHST{len(math_stash)-1}ASH'
        text = re.sub(r'\$\$[\s\S]+?\$\$', _math_stash, text)
        text = re.sub(r'(?<!\$)\$(?!\$)[^$\n]+?(?<!\$)\$(?!\$)', _math_stash, text)

        # Bold: **text** → \textbf{text}
        text = re.sub(r'\*\*(.+?)\*\*', r'\\textbf{\1}', text)
        # Italic: *text* → \textit{text} (수식 밖에서만)
        text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\\textit{\1}', text)
        # _text_ 이탤릭: MD에서만 사용, 수식 _ 와 충돌 방지 위해 단어 경계 강화
        text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'\\textit{\1}', text)
        # Code: `text` → \texttt{text}
        text = re.sub(r'`([^`]+)`', r'\\texttt{\1}', text)

        # 수식 복원
        for i, f in enumerate(math_stash):
            text = text.replace(f'MATHST{i}ASH', f)
        # raw LaTeX 공백 정규화 ($...$ 내부)
        text = _normalize_latex_spaces(text)
        # 인용 [N] → \cite{key}
        def _replace_cite(m):
            nums = re.split(r'[,–\-]+', m.group(1))
            keys = [cite_map.get(n.strip(), f'ref{n.strip()}') for n in nums if n.strip()]
            return r'\cite{' + ','.join(keys) + '}'
        text = re.sub(r'\[(\d+(?:\s*[,–\-]\s*\d+)*)\]', _replace_cite, text)
        # 특수 기호 이스케이프 (&, %, # 등 — 수식 내부 제외)
        text = _escape_latex_safe(text)
        # 인라인 수식 중괄호 불균형 수정
        text = _fix_inline_math(text)
        return text

    def _wrap_orphan_math(line: str) -> str:
        """Wrap \\left(...)\\right) in $...$ if not already in math mode."""
        # Already in display math → skip
        if line.strip().startswith('$$') or line.strip() == '':
            return line
        # Stash existing math, then wrap orphan \\left
        _stash: list[str] = []
        def _s(m):
            _stash.append(m.group(0))
            return f'ORPH{len(_stash)-1}END'
        tmp = re.sub(r'\$\$[\s\S]+?\$\$', _s, line)
        tmp = re.sub(r'(?<!\$)\$(?!\$)[^$\n]+?(?<!\$)\$(?!\$)', _s, tmp)
        # Now wrap orphan \\left
        tmp = re.sub(
            r'\\left\s*([(\[{|\\])(.*?)\\right\s*([)\]|}|\\])',
            lambda m: f'${m.group(0)}$',
            tmp, flags=re.DOTALL
        )
        # Restore
        for idx, s in enumerate(_stash):
            tmp = tmp.replace(f'ORPH{idx}END', s)
        return tmp

    while i < len(lines):
        line = lines[i]

        # ── 빈 줄 ──────────────────────────────────────────────────────────
        if not line.strip():
            result.append('')
            i += 1
            continue

        # ── YAML frontmatter (이미 파싱됨, 스킵) ──────────────────────────
        if line.strip() == '---' and i == 0:
            while i < len(lines) and not (lines[i].strip() == '---' and i > 0):
                i += 1
            i += 1
            continue

        # ── 섹션 헤딩 ──────────────────────────────────────────────────────
        heading_m = re.match(r'^(#{1,4})\s+(.+)', line)
        if heading_m:
            level = len(heading_m.group(1))
            text = heading_m.group(2).strip()

            # 참고문헌 섹션은 별도 처리
            if re.match(r'^References?$', text, re.IGNORECASE):
                # 참고문헌 블록 수집은 뒤에서 처리
                result.append('\\bibliography{references}')
                result.append('\\bibliographystyle{unsrt}')
                # 남은 참고문헌 텍스트 스킵 (thebibliography로 대체)
                i += 1
                # 실제 참고문헌 내용 스킵 (refs는 이미 파싱됨)
                while i < len(lines):
                    nxt = lines[i].strip()
                    # 다음 섹션 헤딩이 오면 중단
                    if re.match(r'^#{1,4}\s+', nxt) and not re.match(r'^#{1,4}\s+References?', nxt, re.IGNORECASE):
                        break
                    i += 1
                continue

            # "1. Title" / "2.1. Title" 형태의 번호 제거
            text_clean = re.sub(r'^\d+(?:\.\d+)*\.?\s+', '', text).strip()

            # ## N.M. 형태 → subsection (소수점 있으면 depth +1)
            has_sub = bool(re.match(r'^\d+\.\d+', text))
            adjusted_level = min(level + (1 if has_sub else 0), 4)

            cmd_map = {1: 'part', 2: 'section', 3: 'subsection', 4: 'subsubsection'}
            cmd = cmd_map.get(adjusted_level, 'paragraph')
            heading_latex = _convert_inline(text_clean)
            # 섹션 제목의 $...$ → \texorpdfstring{$...$}{text} (hyperref 호환)
            def _texorpdf(m):
                math_content = m.group(1)
                # PDF 북마크용 텍스트: LaTeX 명령 제거
                plain = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', math_content)
                plain = re.sub(r'\\[a-zA-Z]+', '', plain).strip()
                return r'\texorpdfstring{$' + math_content + r'$}{' + plain + r'}'
            heading_latex = re.sub(
                r'\$([^$\n]+)\$', _texorpdf, heading_latex
            )
            result.append(f'\\{cmd}{{{heading_latex}}}')
            result.append('')
            i += 1
            continue

        # ── display 수식 $$...$$ ──────────────────────────────────────────
        if line.strip() == '$$':
            eq_lines = []
            i += 1
            while i < len(lines) and lines[i].strip() != '$$':
                eq_lines.append(lines[i])
                i += 1
            i += 1  # 닫는 $$
            latex_content = '\n'.join(eq_lines).strip()
            # display 수식 내부에 중첩된 $...$ 제거 (\mathrm{$f_1$} 등 OCR 오류)
            latex_content = re.sub(r'(?<!\$)\$(?!\$)([^$\n]+?)(?<!\$)\$(?!\$)',
                                   r'\1', latex_content)
            # raw LaTeX 공백 정규화 (\mathrm { a x i a l } → \mathrm{axial})
            latex_content = _fix_latex_math_spaces(latex_content)
            result.append('\\begin{equation}')
            result.append(latex_content)
            result.append('\\end{equation}')
            result.append('')
            continue

        # ── 이미지 ![...](path) ────────────────────────────────────────────
        img_m = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)', line.strip())
        if img_m:
            alt = img_m.group(1)
            src = img_m.group(2)
            src_path = Path(src)

            # 이미지 복사
            fig_counter[0] += 1
            fig_num = fig_counter[0]
            dest_name = f'figure_{fig_num}{src_path.suffix or ".png"}'
            dest_path = figures_dir / dest_name

            # 원본 경로 해석: assets 폴더 기준
            if assets_src and not src_path.is_absolute():
                candidates = [
                    assets_src / src_path,
                    assets_src / src_path.name,
                    Path(src),
                ]
                for c in candidates:
                    if c.exists():
                        shutil.copy2(c, dest_path)
                        copied_figures.append(dest_name)
                        break
                else:
                    # 파일 없음 — 경로만 기록
                    copied_figures.append(f'[MISSING: {src}]')
            elif src_path.exists():
                shutil.copy2(src_path, dest_path)
                copied_figures.append(dest_name)

            # 다음 줄(빈 줄 건너뜀)이 캡션 (*...*)인지 확인
            caption = alt
            look = i + 1
            while look < len(lines) and not lines[look].strip():
                look += 1
            if look < len(lines):
                cap_m = re.match(r'^\*(.+)\*$', lines[look].strip())
                if cap_m:
                    caption = cap_m.group(1).strip()
                    i = look  # 빈 줄 + 캡션 줄 소비

            # Cap very long captions (>500 chars) to prevent Float too large
            caption_text = _convert_inline(caption)
            if len(caption) > 500:
                # Truncate at last sentence boundary before 500 chars,
                # but only at a safe boundary (outside $...$ math)
                def _safe_caption_trunc(cap: str, limit: int = 500) -> str:
                    """Truncate caption at last '. ' before limit, outside math."""
                    trunc = cap[:limit]
                    # Find last '. ' that is outside $...$
                    best = -1
                    in_math = False
                    for ci in range(len(trunc) - 1):
                        if trunc[ci] == '$' and (ci == 0 or trunc[ci-1] != '\\'):
                            in_math = not in_math
                        if not in_math and trunc[ci] == '.' and trunc[ci+1] == ' ':
                            best = ci
                    if best > 200:
                        return cap[:best+1]
                    # Fallback: find last safe non-math position
                    in_math = False
                    last_safe = limit
                    for ci in range(len(trunc)):
                        if trunc[ci] == '$' and (ci == 0 or trunc[ci-1] != '\\'):
                            in_math = not in_math
                        if not in_math:
                            last_safe = ci
                    return cap[:last_safe+1]
                trunc_cap = _safe_caption_trunc(caption)
                caption_text = _convert_inline(trunc_cap) + r' \ldots{}'

            result.append('\\begin{figure}[!htbp]')  # p: float page, 긴 캡션도 수용
            result.append('  \\centering')
            result.append(f'  \\includegraphics[width=0.95\\linewidth,height=0.70\\textheight,keepaspectratio]{{figures/{dest_name}}}')
            result.append(f'  \\caption{{\\small {caption_text}}}')
            result.append(f'  \\label{{fig:figure{fig_num}}}')
            result.append('\\end{figure}')
            result.append('')
            i += 1
            continue

        # ── 캡션 blockquote > Figure N. ... ───────────────────────────────
        bq_m = re.match(r'^>\s*(.+)', line.strip())
        if bq_m:
            # blockquote는 이미 위 이미지 처리에서 흡수됨
            # 독립 blockquote → \begin{quote}
            result.append('\\begin{quote}')
            result.append(_convert_inline(bq_m.group(1)))
            result.append('\\end{quote}')
            i += 1
            continue

        # ── 표 | ... | ────────────────────────────────────────────────────
        if line.strip().startswith('|') and '|' in line[1:]:
            table_lines = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith('|'):
                table_lines.append(lines[i])
                i += 1
            table_counter[0] += 1
            t_num = table_counter[0]
            lat, _ = _md_table_to_latex(table_lines, t_num, figures_dir, assets_src)
            result.append(lat)
            result.append('')
            continue

        # ── 목록 - / * ────────────────────────────────────────────────────
        if re.match(r'^[-*]\s+', line):
            result.append('\\begin{itemize}')
            while i < len(lines) and re.match(r'^[-*]\s+', lines[i]):
                item_text = re.sub(r'^[-*]\s+', '', lines[i])
                result.append(f'  \\item {_convert_inline(item_text)}')
                i += 1
            result.append('\\end{itemize}')
            result.append('')
            continue

        # ── 번호 목록 1. ────────────────────────────────────────────────
        if re.match(r'^\d+\.\s+', line):
            result.append('\\begin{enumerate}')
            while i < len(lines) and re.match(r'^\d+\.\s+', lines[i]):
                item_text = re.sub(r'^\d+\.\s+', '', lines[i])
                result.append(f'  \\item {_convert_inline(item_text)}')
                i += 1
            result.append('\\end{enumerate}')
            result.append('')
            continue

        # ── 수평선 --- ──────────────────────────────────────────────────
        if re.match(r'^---+$', line.strip()):
            result.append('\\hrule')
            i += 1
            continue

        # ── 일반 텍스트 단락 ───────────────────────────────────────────────
        result.append(_convert_inline(_wrap_orphan_math(line)))
        i += 1

    return '\n'.join(result), copied_figures


# ─────────────────────────────────────────────────────────────────────────────
# 저널별 preamble 선택
# ─────────────────────────────────────────────────────────────────────────────

_PREAMBLES: dict[str, str] = {

    'generic': r"""\documentclass[12pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[a4paper, margin=2.5cm]{geometry}
\usepackage{amsmath, amssymb, bm}
\usepackage{graphicx}
\usepackage{float}
\usepackage{caption}
\captionsetup[figure]{font=small,labelfont=bf,skip=4pt}
\usepackage{subcaption}
\usepackage{booktabs}
\usepackage{array}
\usepackage{tabularx}
\usepackage{longtable}
\usepackage[colorlinks=true, linkcolor=blue, citecolor=blue, urlcolor=blue]{hyperref}
\usepackage{cite}
\usepackage{setspace}
\doublespacing
\emergencystretch=5em""",

    'wiley': r"""\documentclass[12pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[a4paper, top=3cm, bottom=3cm, left=3cm, right=3cm]{geometry}
\usepackage{amsmath, amssymb, bm}
\usepackage{graphicx}
\usepackage{float}
\usepackage{caption}
\captionsetup[figure]{font=small,labelfont=bf,skip=4pt}
\usepackage{subcaption}
\usepackage{booktabs}
\usepackage{tabularx}
\usepackage[colorlinks=true]{hyperref}
\usepackage{natbib}
\usepackage{setspace}
\doublespacing
\emergencystretch=5em  % 긴 단어/URL의 Overfull hbox 완화
% Wiley submission: XeLaTeX 권장 (로컬 MikTeX에서는 pdflatex도 가능)""",

    'nature': r"""\documentclass[12pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[a4paper, margin=2.5cm]{geometry}
\usepackage{amsmath, amssymb}
\usepackage{graphicx}
\usepackage{float}
\usepackage{caption}
\usepackage{booktabs}
\usepackage[colorlinks=false]{hyperref}
\usepackage{natbib}
\usepackage{setspace}
\doublespacing
\linespread{1.5}
\emergencystretch=5em
% Nature submission: 12pt, double-spaced, line numbers 권장""",

    'acs': r"""\documentclass{achemso}
% achemso.cls: MikTeX 자동 설치 (첫 컴파일 시 Install 클릭)
\usepackage[T1]{fontenc}
\usepackage{amsmath, amssymb}
\usepackage{graphicx}
\usepackage{float}
\usepackage{booktabs}
% journal= 코드 설정 (예: acsnano, nanolett, acsami)
% \SectionNumbersOn  % 섹션 번호 사용 시""",

    'elsevier': r"""\documentclass[preprint,12pt]{elsarticle}
% elsarticle.cls: MikTeX 자동 설치
\usepackage{amsmath, amssymb}
\usepackage{graphicx}
\usepackage{float}
\usepackage{booktabs}
\usepackage[colorlinks=true]{hyperref}
\biboptions{authoryear}""",
}


def _get_journal_preamble(journal: str) -> str:
    return _PREAMBLES.get(journal.lower(), _PREAMBLES['generic'])


# ─────────────────────────────────────────────────────────────────────────────
# 제목/저자/소속 블록 생성
# ─────────────────────────────────────────────────────────────────────────────

def _escape_affil_with_urls(affil_str: str) -> str:
    """소속 문자열에서 URL을 \\url{}로 감싼 뒤 나머지만 _escape_latex 처리.

    URL은 \\url{} 안에서 자동으로 줄바꿈되므로 Overfull \\hbox를 방지.
    `hyperref` 또는 `url` 패키지가 preamble에 있어야 함.

    OCR 아티팩트로 URL 내에 공백이 삽입된 경우(예: "https://doi.org/ 10.1038/...")도 처리.
    """
    # 1단계: OCR로 URL 중간에 삽입된 공백 제거
    # "https://.../ xxxx" 패턴에서 "/" 다음 공백을 제거
    affil_str = re.sub(r'(https?://\S+?)\s+(\S)', lambda m: m.group(1) + m.group(2), affil_str)
    # 추가로 연속 처리 (중간에 공백이 여러 개인 경우)
    for _ in range(3):
        affil_str = re.sub(r'(https?://\S+?)\s+(\S)', lambda m: m.group(1) + m.group(2), affil_str)

    url_pat = re.compile(r'https?://\S+')
    parts: list[str] = []
    prev = 0
    for m in url_pat.finditer(affil_str):
        # URL 앞 텍스트는 일반 이스케이프
        before = affil_str[prev:m.start()]
        if before:
            parts.append(_escape_latex(before))
        # URL 자체는 \url{} — hyperref가 처리하므로 이스케이프 불필요
        url = m.group(0).rstrip('.,;)')  # 마지막 구두점 제거
        trail = m.group(0)[len(url):]    # 잘려나간 구두점
        parts.append(r'\url{' + url + r'}')
        if trail:
            parts.append(_escape_latex(trail))
        prev = m.end()
    # 나머지
    tail = affil_str[prev:]
    if tail:
        parts.append(_escape_latex(tail))
    return ''.join(parts)


def _build_title_block(meta: dict, journal: str,
                       body_affiliations: list[str] | None = None) -> str:
    title = meta.get('title', 'Untitled')
    authors_raw = meta.get('authors', [])
    affiliations = meta.get('affiliations', [])
    email = meta.get('email', '')
    keywords = meta.get('keywords', [])

    # 저자 리스트
    if isinstance(authors_raw, list):
        authors_str = ', '.join(authors_raw)
    else:
        authors_str = str(authors_raw)

    # 소속: YAML affiliations 우선, 없으면 본문에서 캡처한 것 사용
    if not affiliations and body_affiliations:
        affiliations = body_affiliations

    if isinstance(affiliations, list):
        affil_str = '\\\\\n'.join(affiliations[:4])  # 최대 4개
    else:
        affil_str = str(affiliations) if affiliations else ''

    if journal in ('acs',):
        # achemso 스타일 (preamble에 \begin{document} 없음 → 여기서 추가)
        lines = [
            '\\begin{document}',
            f'\\title{{{_escape_latex(title)}}}',
        ]
        if isinstance(authors_raw, list):
            for a in authors_raw:
                lines.append(f'\\author{{{_escape_latex(a)}}}')
        if affiliations and isinstance(affiliations, list) and affiliations:
            a0 = affiliations[0]
            lines.append(f'\\affiliation{{{_escape_latex(str(a0))}}}')
        if email:
            lines.append(f'\\email{{{email}}}')
        lines.append('\\maketitle')
        return '\n'.join(lines) + '\n'

    elif journal in ('elsevier',):
        # elsarticle 스타일
        lines = [
            f'\\title{{{_escape_latex(title)}}}',
            f'\\author{{{_escape_latex(authors_str)}}}',
        ]
        if affil_str:
            lines.append(f'\\address{{{_escape_latex(affil_str)}}}')
        if email:
            lines.append(f'\\ead{{{email}}}')
        lines.append('\\begin{document}')
        lines.append('\\maketitle')
        return '\n'.join(lines) + '\n'

    else:
        # generic / wiley / nature: article 클래스
        # \begin{document}는 preamble 뒤에 한 번만 → 여기서는 추가 안 함
        lines = [f'\\title{{{_escape_latex(title)}}}']

        # 저자 목록: YAML authors가 제목 텍스트로 오염된 경우 필터링
        if isinstance(authors_raw, list):
            author_list = [a for a in authors_raw
                           if not str(a).strip().startswith('##')
                           and not str(a).strip().startswith('#')]
        else:
            author_list = [a.strip() for a in str(authors_str).split(',')
                           if a.strip() and not a.strip().startswith('#')]

        if not author_list:
            author_list = ['Author(s)']

        # 3명 단위로 줄바꿈 (overfull hbox 방지)
        # LaTeX \author{} 내에서 \n은 공백 처리 → \\ + 줄바꿈 필요
        if len(author_list) > 3:
            chunks = [author_list[j:j+3] for j in range(0, len(author_list), 3)]
            authors_fmt = (',\\\\\n').join(', '.join(c) for c in chunks)
        else:
            authors_fmt = ', '.join(author_list)

        lines.append(f'\\author{{{_escape_latex(authors_fmt)}}}')
        lines.append('\\date{}')
        lines.append('\\maketitle')
        if affil_str:
            # 소속은 \maketitle 뒤 flushleft 환경으로 배치.
            # \thanks{}를 \author{} 안에 넣으면 \hbox 전체 폭이 커져
            # Overfull \hbox 발생 → \maketitle 뒤 분리 출력으로 해결.
            affil_escaped = _escape_affil_with_urls(affil_str)
            lines.append(
                r'\begin{flushleft}\footnotesize '
                + affil_escaped +
                r'\end{flushleft}'
            )
        if keywords and (isinstance(keywords, list) and len(keywords) > 0):
            kw_str = ', '.join(keywords) if isinstance(keywords, list) else str(keywords)
            lines.append(f'\\noindent\\textbf{{Keywords:}} {_escape_latex(kw_str)}\n')
        return '\n'.join(lines) + '\n'


# ─────────────────────────────────────────────────────────────────────────────
# Abstract 추출
# ─────────────────────────────────────────────────────────────────────────────

def _remove_header_block(body: str, meta: dict) -> tuple[str, list[str]]:
    """본문 앞의 저자/소속 헤더 블록 제거. 캡처된 소속 목록도 함께 반환.

    hybrid_v8 MD 구조:
      ## Title          ← 제거
      저자줄            ← 제거
      Abstract 단락     ← 유지 (나중에 _extract_abstract가 처리)
      소속1             ← 제거 + 캡처
      소속2             ← 제거 + 캡처
      ## 1. Introduction  ← 여기서 헤더 종료

    반환: (본문, 소속_줄_목록)
    """
    # 소속/기관 키워드 패턴
    _AFFIL_PAT = re.compile(
        r'(?:University|Institute|College|Laboratory|Department|School of|'
        r'Center|Republic of Korea|Republic of China|Japan|USA|Germany|'
        r'Seoul|Daejeon|Daegu|Busan|Suwon|Incheon|Korea\s*\d|'
        r'\d{5},\s*Republic|\b\d{5}\b)',
        re.IGNORECASE
    )
    # 저자 약어 패턴: "J.-C. Choi, H. Y . Jeong, ..." — 이니셜+성 2명 이상
    _au = r'[A-Z][\.\-][A-Z\.\- ]{0,5}\s+[A-Z][a-z][A-Za-z\-]+'
    _AUTHOR_ABBR_PAT = re.compile(
        r'^(?:' + _au + r')(?:,\s*(?:' + _au + r')){1,}'
    )
    # Science Advances 형식: "First Last 1 , First Last 2 , ..." (각주 번호 포함)
    _AUTHOR_NUM_PAT = re.compile(
        r'^[A-Z][a-z]+\s+[A-Z][a-z]+\s*\d+\s*,\s*[A-Z][a-z]+\s+[A-Z][a-z]+'
    )
    # YAML 저자 목록으로 저자줄 감지
    authors_from_meta: list[str] = []
    for a in (meta.get('authors', []) or []):
        a_s = str(a).strip()
        if a_s:
            authors_from_meta.append(a_s.lower())

    def _is_author_line(s: str) -> bool:
        """저자 이름이 2명 이상 포함된 줄인지 확인."""
        if not authors_from_meta:
            return False
        s_lower = s.lower()
        matches = sum(1 for a in authors_from_meta if a in s_lower)
        return matches >= 2

    lines = body.split('\n')
    result = []
    captured_affils: list[str] = []   # 캡처된 소속 줄들
    in_header = True
    title_heading_removed = False

    # 소속 줄들을 연속으로 묶어 하나의 항목으로 합치기 위한 버퍼
    affil_buf: list[str] = []

    def _flush_affil_buf():
        """버퍼에 쌓인 소속 줄들을 하나로 합쳐 captured_affils에 추가."""
        if affil_buf:
            merged = ' '.join(affil_buf).strip()
            if merged:
                captured_affils.append(merged)
            affil_buf.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not in_header:
            result.append(line)
            i += 1
            continue

        # 논문 섹션 헤딩 패턴 (Introduction, Methods, Results 등)
        _SECTION_PAT = re.compile(
            r'^(?:\d+[\.\s]*)?(?:Introduction|Background|Methods?|'
            r'Materials?\s+and\s+Methods?|Results?|Discussion|Conclusion|'
            r'Experimental|Supporting|Acknowledgem|References?|INTRODUCTION|'
            r'RESULTS?|DISCUSSION|METHODS?|CONCLUSIONS?|'
            r'ARTICLES?|Online\s+content|Supplementary|Data\s+availability|'
            r'Author\s+contributions?|Competing\s+interests?)\b',
            re.IGNORECASE
        )

        # 헤딩 처리: ## / ### 모두
        if stripped.startswith('#'):
            heading_text = re.sub(r'^#+\s+', '', stripped).strip()
            heading_level = len(re.match(r'^(#+)', stripped).group(1))

            # 아직 첫 제목 헤딩을 못 만남 → 제목 헤딩 제거
            if not title_heading_removed:
                title_heading_removed = True
                i += 1
                continue

            # 실제 섹션 헤딩 (Introduction 등) → 헤더 존 종료
            if _SECTION_PAT.match(heading_text):
                _flush_affil_buf()
                in_header = False
                if result and result[-1].strip():
                    result.append('')
                result.append(line)
                i += 1
                continue

            # 아직 헤더 존 내의 ## 헤딩 (추가 제목줄, Science Advances ENGINEERING 등) → 제거
            i += 1
            continue

        if title_heading_removed:
            # 빈 줄 → 소속 버퍼 flush (새 소속 그룹 경계)
            if not stripped:
                _flush_affil_buf()

            # 소속/기관 키워드 포함 줄 → 캡처 후 제거
            elif _AFFIL_PAT.search(stripped):
                affil_buf.append(stripped)

            # 저자 약어 패턴 줄 → 소속 그룹의 시작 (저자명 + 기관명이 같은 줄에 있는 경우)
            elif _AUTHOR_ABBR_PAT.match(stripped):
                # 이 줄에 소속 키워드도 포함됐으면 소속 버퍼에 추가
                if _AFFIL_PAT.search(stripped):
                    affil_buf.append(stripped)
                else:
                    _flush_affil_buf()
                    affil_buf.append(stripped)  # 다음 줄 소속과 묶임

            # Science Advances 형식 저자줄: "First Last 1 , First Last 2 , ..."
            elif _AUTHOR_NUM_PAT.match(stripped):
                _flush_affil_buf()  # 소속 버퍼 flush, 저자줄 자체는 제거

            # YAML 저자 이름 2명 이상 포함된 줄 → 제거 (저자줄)
            elif _is_author_line(stripped):
                _flush_affil_buf()

            # 나머지 → 유지 (Abstract 텍스트 등), 단락 경계 빈 줄 보존
            else:
                _flush_affil_buf()
                # 이전이 비어있지 않으면 빈 줄 삽입 (단락 구분 복원)
                if result and result[-1].strip():
                    result.append('')
                result.append(line)

        i += 1

    _flush_affil_buf()
    return '\n'.join(result), captured_affils


def _extract_abstract(body: str) -> tuple[str, str]:
    """Abstract 섹션 추출. (abstract_text, body_without_abstract) 반환."""
    # ## Abstract 섹션 탐색
    m = re.search(
        r'(?:^#{1,3}\s+Abstract\s*\n)([\s\S]+?)(?=\n#{1,3}\s+|\Z)',
        body, re.IGNORECASE | re.MULTILINE
    )
    if m:
        abstract = m.group(1).strip()
        body_clean = body[:m.start()] + body[m.end():]
        return abstract, body_clean.strip()

    # Abstract 키워드 없이 첫 긴 단락 (150자 이상, 헤딩/이미지/표 제외)
    paras = body.split('\n\n')
    for i, p in enumerate(paras):
        p_stripped = p.strip()
        if (len(p_stripped) > 150
                and not p_stripped.startswith('#')
                and not p_stripped.startswith('!')
                and not p_stripped.startswith('$')
                and not p_stripped.startswith('|')):
            # 단락 내에 헤딩이 포함된 경우 헤딩 전까지만 Abstract
            heading_in_para = re.search(r'\n#+\s+', p_stripped)
            if heading_in_para:
                abstract = p_stripped[:heading_in_para.start()].strip()
                rest_of_para = p_stripped[heading_in_para.start():].strip()
                remaining = rest_of_para + ('\n\n' + '\n\n'.join(paras[i+1:]) if paras[i+1:] else '')
                return abstract, remaining.strip()
            # 단락 내 이미지가 있으면 이미지 이전까지만 Abstract로
            img_in_para = re.search(r'\n!\[', p_stripped)
            if img_in_para:
                abstract = p_stripped[:img_in_para.start()].strip()
                rest_of_para = p_stripped[img_in_para.start():]
                remaining = rest_of_para
                if paras[i+1:]:
                    remaining += '\n\n' + '\n\n'.join(paras[i+1:])
                return abstract, remaining.strip()
            # Abstract가 너무 길면 (3000자 초과) 본문이 섞인 것으로 판단 → 첫 문장~단락만
            if len(p_stripped) > 3000:
                # 첫 빈 줄 기준으로 분리
                first_break = p_stripped.find('\n\n')
                if first_break > 150:
                    abstract = p_stripped[:first_break].strip()
                    rest = p_stripped[first_break:]
                    remaining = rest + ('\n\n' + '\n\n'.join(paras[i+1:]) if paras[i+1:] else '')
                    return abstract, remaining.strip()
                # 첫 빈 줄 없으면 첫 섹션 헤딩 전까지
                sec_m = re.search(r'\n#+\s+', p_stripped)
                if sec_m:
                    abstract = p_stripped[:sec_m.start()].strip()
                    rest = p_stripped[sec_m.start():]
                    remaining = rest + ('\n\n' + '\n\n'.join(paras[i+1:]) if paras[i+1:] else '')
                    return abstract, remaining.strip()
            return p_stripped, '\n\n'.join(paras[i+1:]).strip()

    return '', body


def _extract_references_block(body: str) -> tuple[str, str]:
    """References 섹션 추출. (ref_text, body_without_refs) 반환."""
    m = re.search(
        r'(?:^#{1,3}\s+References?\s*\n)([\s\S]+?)(?=\n#{1,3}\s+|\Z)',
        body, re.IGNORECASE | re.MULTILINE
    )
    if m:
        return m.group(1).strip(), body[:m.start()].strip()

    # "References" 헤딩 없이 [1] 패턴으로 시작하는 블록
    last = list(re.finditer(r'\n\[\d+\]', body))
    if last:
        start = last[0].start()
        ref_block_raw = body[start:]
        end_m = re.search(r'\n(?:---+|#{1,3}\s+[^R])', ref_block_raw)
        if end_m:
            ref_block_raw = ref_block_raw[:end_m.start()]
        return ref_block_raw.strip(), body[:start].strip()

    # Science Advances 등: "1. Author, ..." 번호 목록 형식
    last2 = list(re.finditer(r'\n1\. [A-Z]', body))
    if last2:
        start = last2[-1].start()  # 마지막 등장 위치 (본문 목록과 구분)
        ref_block_raw = body[start:]
        end_m = re.search(r'\n(?:---+|#{1,3}\s+)', ref_block_raw)
        if end_m:
            ref_block_raw = ref_block_raw[:end_m.start()]
        # 최소 3개 항목이 있어야 참고문헌으로 인정
        if len(re.findall(r'^\d+\. ', ref_block_raw, re.MULTILINE)) >= 3:
            return ref_block_raw.strip(), body[:start].strip()

    return '', body


# ─────────────────────────────────────────────────────────────────────────────
# 메인 변환 함수
# ─────────────────────────────────────────────────────────────────────────────

def convert_md_to_latex(
    md_path: Path,
    out_dir: Path,
    journal: str = 'generic',
) -> dict:
    """hybrid_v8 MD → LaTeX + BibTeX 변환.

    Returns:
        {'tex': Path, 'bib': Path, 'figures': list[str], 'warnings': list[str]}
    """
    md_text = md_path.read_text(encoding='utf-8')
    warnings: list[str] = []

    # ── YAML 파싱 ────────────────────────────────────────────────────────────
    meta, body = parse_yaml_frontmatter(md_text)

    # assets 폴더 위치 (이미지 복사용)
    assets_name = md_path.stem.replace('_Hybrid_Full', '') + '_Hybrid_assets'
    assets_dir = md_path.parent / assets_name
    if not assets_dir.exists():
        # 일반 assets 폴더 탐색
        candidates = list(md_path.parent.glob('*_assets')) + list(md_path.parent.glob('*_Hybrid_assets'))
        assets_dir = candidates[0] if candidates else None

    # ── 본문 헤더 블록 제거 + 소속 캡처 ────────────────────────────────────
    body, body_affiliations = _remove_header_block(body, meta)

    # ── 참고문헌 추출 + BibTeX 생성 ──────────────────────────────────────────
    ref_text, body = _extract_references_block(body)
    refs = _parse_references(ref_text) if ref_text else []

    # BibTeX 키 미리 설정 (cite_map용)
    seen: dict[str, int] = {}
    for r in refs:
        key = r['key']
        if key in seen:
            seen[key] += 1
            r['_bib_key'] = f"{key}{chr(96 + seen[key])}"
        else:
            seen[key] = 1
            r['_bib_key'] = key

    bib_text = _refs_to_bibtex(refs)

    # ── Abstract 추출 ────────────────────────────────────────────────────────
    abstract_text, body = _extract_abstract(body)

    # ── figures 폴더 ─────────────────────────────────────────────────────────
    figures_dir = out_dir / 'figures'

    # ── 본문 변환 ────────────────────────────────────────────────────────────
    latex_body, copied_figures = _convert_body(body, figures_dir, assets_dir, refs)

    # ── 전체 .tex 조립 ───────────────────────────────────────────────────────
    preamble = _get_journal_preamble(journal)
    title_block = _build_title_block(meta, journal, body_affiliations)

    abstract_block = ''
    if abstract_text:
        # Wrap abstract text at ~80 chars for LaTeX readability (prevents Overfull hbox)
        # Must preserve math expressions ($...$) intact — don't break inside them
        def _wrap_text(text: str, width: int = 80) -> str:
            # Stash math tokens so textwrap doesn't break inside them
            _math_stash: list[str] = []
            def _stash_math(m):
                placeholder = f'WRAPMATH{len(_math_stash):04d}X'
                _math_stash.append(m.group(0))
                return placeholder
            tmp = re.sub(r'\$\$[\s\S]+?\$\$', _stash_math, text)
            tmp = re.sub(r'(?<!\$)\$(?!\$)[^$\n]+?(?<!\$)\$(?!\$)', _stash_math, tmp)
            import textwrap
            wrapped = '\n'.join(textwrap.wrap(tmp, width=width, break_long_words=False, break_on_hyphens=False))
            for idx, m in enumerate(_math_stash):
                wrapped = wrapped.replace(f'WRAPMATH{idx:04d}X', m)
            return wrapped
        abstract_latex = _escape_latex_safe(_wrap_text(abstract_text))
        # \sloppy + \tolerance + \exhyphenpenalty: abstract 내 긴 수식/URL/복합어 Overfull 방지
        # \exhyphenpenalty=0: 복합어 하이픈("high-performance") 뒤에서 줄바꿈 허용
        abstract_block = (f'\\begin{{abstract}}\n'
                          f'\\sloppy\\tolerance=9999\\exhyphenpenalty=0\n'
                          f'{abstract_latex}\n\\fussy\n\\end{{abstract}}\n\n')

    # \begin{document} 위치:
    #   generic/wiley/nature: preamble 바로 뒤
    #   acs: _build_title_block 안에 포함
    #   elsevier: _build_title_block 안에 포함
    if journal in ('acs', 'elsevier'):
        tex_content = (
            preamble + '\n\n'
            + title_block + '\n'
            + abstract_block
            + latex_body + '\n\n'
            + '\\end{document}\n'
        )
    else:
        # generic / wiley / nature: \begin{document} 여기서 삽입
        tex_content = (
            preamble + '\n\n'
            + '\\begin{document}\n\n'
            + title_block + '\n'
            + abstract_block
            + latex_body + '\n\n'
            + '\\end{document}\n'
        )

    # ── 저장 ────────────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / 'submission.tex'
    bib_path = out_dir / 'references.bib'

    tex_path.write_text(tex_content, encoding='utf-8')
    bib_path.write_text(bib_text, encoding='utf-8')

    # 누락 이미지 경고
    for f in copied_figures:
        if f.startswith('[MISSING:'):
            warnings.append(f'이미지 없음: {f}')

    if not refs:
        warnings.append('참고문헌을 찾지 못했습니다 — references.bib 수동 확인 필요')

    print(f'[md_to_latex] .tex 생성: {tex_path}')
    print(f'[md_to_latex] .bib 생성: {bib_path} ({len(refs)}개 항목)')
    print(f'[md_to_latex] 이미지 복사: {len([f for f in copied_figures if not f.startswith("[")])}개')
    if warnings:
        for w in warnings:
            print(f'[md_to_latex] ⚠ {w}')

    return {
        'tex':      tex_path,
        'bib':      bib_path,
        'figures':  copied_figures,
        'warnings': warnings,
        'n_refs':   len(refs),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MikTeX 컴파일
# ─────────────────────────────────────────────────────────────────────────────

def compile_latex(tex_path: Path, n_runs: int = 3) -> bool:
    """MikTeX pdflatex로 .tex 컴파일 (3회 실행으로 크로스레퍼런스 완성)."""
    import subprocess

    # MikTeX pdflatex 경로 탐색
    pdflatex_candidates = [
        Path(r'C:\Users\Simulation Notebook1\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe'),
        Path(r'C:\Program Files\MiKTeX\miktex\bin\x64\pdflatex.exe'),
        Path(r'C:\Program Files (x86)\MiKTeX\miktex\bin\x64\pdflatex.exe'),
    ]
    pdflatex = None
    for p in pdflatex_candidates:
        if p.exists():
            pdflatex = p
            break

    if pdflatex is None:
        # PATH에서 탐색
        import shutil as _shutil
        found = _shutil.which('pdflatex')
        if found:
            pdflatex = Path(found)
        else:
            print('[compile] pdflatex를 찾을 수 없습니다. MikTeX 설치 확인 필요.')
            return False

    cwd = tex_path.parent
    for run in range(1, n_runs + 1):
        print(f'[compile] pdflatex 실행 {run}/{n_runs}...')
        result = subprocess.run(
            [str(pdflatex), '-interaction=batchmode', tex_path.name],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # batchmode에서는 오류가 있어도 계속 진행 (PDF 부분 생성 가능)
            print(f'[compile] 경고: 컴파일 오류 있음 (run {run}/{n_runs})')

    # BibTeX 실행 (참고문헌 처리)
    bib_result = subprocess.run(
        ['bibtex', tex_path.stem],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if bib_result.returncode == 0:
        print('[compile] bibtex 완료')
        # 참고문헌 포함 재컴파일 (2회)
        for run in range(1, 3):
            subprocess.run(
                [str(pdflatex), '-interaction=batchmode', tex_path.name],
                cwd=cwd, capture_output=True, text=True,
            )

    pdf_path = cwd / (tex_path.stem + '.pdf')
    if pdf_path.exists():
        print(f'[compile] PDF 생성 완료: {pdf_path}')
        # 컴파일 오류 요약 출력
        log_path = cwd / (tex_path.stem + '.log')
        if log_path.exists():
            log_text = log_path.read_text(encoding='utf-8', errors='ignore')
            errors = re.findall(r'! .+', log_text)
            if errors:
                unique_errors = list(dict.fromkeys(errors))[:5]
                print(f'[compile] ⚠ 수식 오류 {len(errors)}개 (무시되고 PDF 생성됨):')
                for e in unique_errors:
                    print(f'  {e[:80]}')
        return True
    else:
        print(f'[compile] PDF 생성 실패: {pdf_path}')
        # 오류 요약 출력
        log_path = cwd / (tex_path.stem + '.log')
        if log_path.exists():
            log_text = log_path.read_text(encoding='utf-8', errors='ignore')
            errors = re.findall(r'! .+', log_text)
            if errors:
                print(f'[compile] 오류 목록 (처음 5개):')
                for e in errors[:5]:
                    print(f'  {e}')
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='hybrid_v8 MD → LaTeX 변환')
    parser.add_argument('md', help='입력 MD 파일 (_Hybrid_Full.md)')
    parser.add_argument('--journal', default='generic',
                        choices=['generic', 'wiley', 'nature', 'acs', 'elsevier'],
                        help='저널 스타일 (기본: generic)')
    parser.add_argument('--out-dir', help='출력 디렉토리 (기본: MD 파일 옆 latex/)')
    parser.add_argument('--compile', action='store_true',
                        help='MikTeX pdflatex으로 컴파일')
    parser.add_argument('--no-compile', action='store_true',
                        help='컴파일 건너뜀 (기본)')
    args = parser.parse_args()

    md_path = Path(args.md).resolve()
    if not md_path.exists():
        print(f'오류: MD 파일 없음: {md_path}')
        sys.exit(1)

    out_dir = Path(args.out_dir).resolve() if args.out_dir else md_path.parent / 'latex'

    print('=' * 60)
    print(f'MD → LaTeX 변환: {md_path.name}')
    print(f'저널 스타일: {args.journal}')
    print(f'출력 경로: {out_dir}')
    print('=' * 60)

    result = convert_md_to_latex(md_path, out_dir, journal=args.journal)

    if args.compile:
        print('\n[컴파일 시작]')
        compile_latex(result['tex'])

    print('\n완료!')
    print(f"  .tex: {result['tex']}")
    print(f"  .bib: {result['bib']} ({result['n_refs']}개 참고문헌)")
    if result['warnings']:
        print(f"  경고 {len(result['warnings'])}개:")
        for w in result['warnings']:
            print(f"    - {w}")


if __name__ == '__main__':
    main()
