"""
engines/text_normalize.py — 공유 텍스트 정규화 모듈

PDF 추출 마크다운의 공통 정규화 단계를 하나의 함수로 통합.
run_paper_hybrid.py의 normalize_docling_md()와 _normalize_span_text()가
이 모듈의 normalize_text()를 호출하여 중복 로직을 제거.

Steps:
  1. 리가처 → ASCII
  2. Docling /Cxx 이스케이프
  3. 특수 공백 → 일반 공백
  3.5. Symbol 폰트 PUA → 유니코드
  4. 이중 공백 → 단일 공백
  5. 인용 공백 제거: ( 1 ) → (1)
  5.5. PDF 공백 글리프 이름 삭제
  6. /uXXXX 유니코드 이스케이프 → 실제 문자
  7. µ + 공백 + 단위 → µ단위
  8.5. 참고문헌 볼륨/페이지 공백 정규화
"""

import re

# ─── PDF 리가처 (Docling이 /uniFBxx 형식 문자열로 출력) ────────────────────
LIGATURES: dict[str, str] = {
    '/uniFB00': 'ff',
    '/uniFB01': 'fi',
    '/uniFB02': 'fl',
    '/uniFB03': 'ffi',
    '/uniFB04': 'ffl',
    # 실제 Unicode 문자로도 처리
    '\uFB00': 'ff',
    '\uFB01': 'fi',
    '\uFB02': 'fl',
    '\uFB03': 'ffi',
    '\uFB04': 'ffl',
}

# Docling의 /Cxx 이스케이프 → 의미 있는 문자
DOCLING_ESCAPES: dict[str, str] = {
    '/C0 ': '-',   # 하이픈 (공백 포함)
    '/C0': '-',    # 하이픈
}

# Symbol 폰트 PUA (Private Use Area) 문자 → 유니코드 그리스/특수문자 복원
# 일부 PDF (Science Advances 등)에서 Symbol 폰트를 U+F000 오프셋으로 인코딩
# 규칙: PUA = 0xF000 + Symbol 폰트 ASCII 코드 (Symbol 'a'=α, 'b'=β, ..., 'W'=Ω 등)
SYMBOL_PUA: dict[str, str] = {
    # 소문자 그리스 (Symbol 소문자 → 그리스 소문자)
    '\uf061': 'α',  # a → alpha
    '\uf062': 'β',  # b → beta
    '\uf063': 'χ',  # c → chi
    '\uf064': 'δ',  # d → delta
    '\uf065': 'ε',  # e → epsilon  (C2 논문 strain)
    '\uf066': 'φ',  # f → phi
    '\uf067': 'γ',  # g → gamma
    '\uf068': 'η',  # h → eta
    '\uf069': 'ι',  # i → iota
    '\uf06a': 'ϕ',  # j → phi (variant)
    '\uf06b': 'κ',  # k → kappa
    '\uf06c': 'λ',  # l → lambda  (C2 논문 wavelength)
    '\uf06d': 'µ',  # m → mu/micro (C2 논문 µm)
    '\uf06e': 'ν',  # n → nu
    '\uf06f': 'ο',  # o → omicron
    '\uf070': 'π',  # p → pi
    '\uf071': 'θ',  # q → theta  (C2 논문 angle)
    '\uf072': 'ρ',  # r → rho
    '\uf073': 'σ',  # s → sigma
    '\uf074': 'τ',  # t → tau
    '\uf075': 'υ',  # u → upsilon
    '\uf077': 'ω',  # w → omega
    '\uf078': 'ξ',  # x → xi
    '\uf079': 'ψ',  # y → psi
    '\uf07a': 'ζ',  # z → zeta
    # 대문자 그리스 (Symbol 대문자 → 그리스 대문자)
    '\uf041': 'Α',  # A → Alpha
    '\uf042': 'Β',  # B → Beta
    '\uf043': 'Χ',  # C → Chi
    '\uf044': 'Δ',  # D → Delta
    '\uf045': 'Ε',  # E → Epsilon
    '\uf046': 'Φ',  # F → Phi
    '\uf047': 'Γ',  # G → Gamma
    '\uf048': 'Η',  # H → Eta
    '\uf049': 'Ι',  # I → Iota
    '\uf04b': 'Κ',  # K → Kappa
    '\uf04c': 'Λ',  # L → Lambda
    '\uf04d': 'Μ',  # M → Mu
    '\uf04e': 'Ν',  # N → Nu
    '\uf04f': 'Ο',  # O → Omicron
    '\uf050': 'Π',  # P → Pi
    '\uf051': 'Θ',  # Q → Theta
    '\uf052': 'Ρ',  # R → Rho
    '\uf053': 'Σ',  # S → Sigma
    '\uf054': 'Τ',  # T → Tau
    '\uf055': 'Υ',  # U → Upsilon
    '\uf057': 'Ω',  # W → Omega  (C2 논문 resistance Ω)
    '\uf058': 'Ξ',  # X → Xi
    '\uf059': 'Ψ',  # Y → Psi
    '\uf05a': 'Ζ',  # Z → Zeta
}


def _replace_unicode_escape(m: re.Match) -> str:
    """정규식 콜백: /uXXXX 이스케이프를 실제 유니코드 문자로 변환."""
    try:
        return chr(int(m.group(1), 16))
    except (ValueError, OverflowError):
        return m.group(0)


def normalize_text(text: str, *, merge_paragraphs: bool = False) -> str:
    """PDF 추출 텍스트에 대한 공통 정규화 단계 적용.

    Args:
        text: 정규화할 텍스트
        merge_paragraphs: True이면 Step 9~10 (Wiley 헤더 제거, 단락 병합) 포함.
                          normalize_docling_md() 전용. span 정규화에서는 False.

    Returns:
        정규화된 텍스트
    """
    # Step 1: 리가처 → ASCII
    for lig, ascii_rep in LIGATURES.items():
        text = text.replace(lig, ascii_rep)

    # Step 2: Docling /Cxx 이스케이프
    for esc, rep in DOCLING_ESCAPES.items():
        text = text.replace(esc, rep)

    # Step 3: NO-BREAK SPACE → 일반 공백
    text = text.replace('\u00A0', ' ')
    text = text.replace('\u2009', ' ')  # thin space
    text = text.replace('\u200A', ' ')  # hair space

    # Step 3.5: Symbol 폰트 PUA 문자 → 유니코드 복원
    for pua, uni in SYMBOL_PUA.items():
        text = text.replace(pua, uni)

    # Step 4: 이중 공백 → 단일 공백
    if merge_paragraphs:
        # 표/코드 블록 내부는 제외
        in_code = False
        lines = []
        for line in text.split('\n'):
            if line.startswith('```'):
                in_code = not in_code
            if not in_code and not line.startswith('|'):
                line = re.sub(r' {2,}', ' ', line)
            lines.append(line)
        text = '\n'.join(lines)
    else:
        # span 텍스트: 단순 치환
        text = re.sub(r' {2,}', ' ', text)

    # Step 5: 인용 공백 제거: ( 1 ) → (1)
    text = re.sub(
        r'\(\s+(\d+(?:\s*[-–]\s*\d+)*(?:,\s*\d+)*)\s+\)',
        r'(\1)', text
    )

    # Step 5.5: PDF 공백 글리프 이름 → 삭제 (/hairspace, /thinspace 등 Docling 아티팩트)
    text = re.sub(r'/(?:hair|thin|en|em|nb|zerowidth)space', '', text, flags=re.IGNORECASE)

    # Step 6: /uXXXX 유니코드 이스케이프 → 실제 문자
    #   5자리 코드포인트 우선 처리 (예: /u1D708 = 𝜈)
    text = re.sub(r'/u([0-9A-Fa-f]{5})', _replace_unicode_escape, text)
    text = re.sub(r'/u([0-9A-Fa-f]{4})', _replace_unicode_escape, text)

    # Step 7: µ + 공백 + 단위문자 → µ단위 (µ m → µm)
    text = re.sub(r'µ ([mMlLsSnNgGΩ])([a-zA-Z])', r'µ\1 \2', text)
    text = re.sub(r'µ ([mMlLsSnNgGΩ])(?=[^a-zA-Z])', r'µ\1', text)

    # Step 8.5: 참고문헌 볼륨/페이지 공백 정규화: "40 , 198-208" → "40, 198-208"
    text = re.sub(r'(?<=\d) , (?=\d)', ', ', text)

    if not merge_paragraphs:
        return text

    # ── 아래 단계는 merge_paragraphs=True (normalize_docling_md 전용) ──

    # Step 9: 다운로드/접근 불필요 정보 제거 (Wiley 다운로드 헤더)
    text = re.sub(
        r'\n\d+\w+(?:\s+\w+)*, \d{4}, \d+, Downloaded from https://[^\n]+\n',
        '\n', text
    )
    text = re.sub(
        r'\d{4}(?:x|X)?\d+x?, \d{4}, \d+, Downloaded from.*?(?:\n|$)',
        '', text
    )

    # Step 10: PDF 컬럼/페이지 경계로 분리된 단락 병합
    def _is_text_para(p: str) -> bool:
        s = p.strip()
        if not s:
            return False
        return not (
            s.startswith('#') or      # 제목
            s.startswith('![') or     # 이미지
            s.startswith('*') or      # 캡션 (이탤릭)
            s.startswith('<!--') or   # 수식 플레이스홀더
            s.startswith('|') or      # 표
            s.startswith('-') or      # 목록
            s.startswith('>')         # 인용
        )

    paras = text.split('\n\n')
    merged_paras: list = []
    i = 0
    while i < len(paras):
        cur = paras[i]
        while i + 1 < len(paras):
            nxt = paras[i + 1]
            if not (_is_text_para(cur) and _is_text_para(nxt)):
                break
            cur_s = cur.strip()
            nxt_s = nxt.strip()
            if not cur_s or cur_s[-1] in '.?!:':
                break
            starts_lower = bool(nxt_s) and nxt_s[0].islower()
            starts_paren = len(nxt_s) > 1 and nxt_s[0] == '(' and nxt_s[1].isupper()
            if not (starts_lower or starts_paren):
                break
            cur = cur.rstrip() + ' ' + nxt_s
            i += 1
        merged_paras.append(cur)
        i += 1
    text = '\n\n'.join(merged_paras)

    return text
