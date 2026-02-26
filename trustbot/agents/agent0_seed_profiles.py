"""
Seed / fallback language profiles.

These are direct translations of the hardcoded patterns previously scattered
across chunker.py, llm_call_extractor.py, and structural_chunker.py.  They
serve as the fallback when Agent 0's LLM profile generation is unavailable
or fails.

Each profile captures exactly the same regex patterns, LLM prompts,
skip-tokens, and block rules that existed in the original hardcoded code.
"""

from __future__ import annotations

from trustbot.models.language_profile import (
    BlockRuleConfig,
    ForwardDeclarationConfig,
    LanguageProfile,
    SpecialFileConfig,
)

# ---------------------------------------------------------------------------
# Helper to build a profile concisely
# ---------------------------------------------------------------------------


def _p(
    language: str,
    *,
    aliases: list[str] | None = None,
    extensions: list[str],
    func_pats: list[str],
    class_pats: list[str] | None = None,
    named_groups: dict[str, str] | None = None,
    fwd_decl: ForwardDeclarationConfig | None = None,
    special_files: list[SpecialFileConfig] | None = None,
    block_rules: list[BlockRuleConfig] | None = None,
    llm_prompt: str = "",
    skip_tokens: list[str] | None = None,
    bare_ids: bool = False,
    bare_lookahead: str = "",
    call_kw_patterns: list[str] | None = None,
    single_comment: str = "//",
    ml_open: str = "/*",
    ml_close: str = "*/",
    string_delims: list[str] | None = None,
) -> LanguageProfile:
    return LanguageProfile(
        language=language,
        aliases=aliases or [],
        file_extensions=extensions,
        function_def_patterns=func_pats,
        class_def_patterns=class_pats or [],
        named_regex_groups=named_groups or {"name": "name"},
        forward_declaration_rules=fwd_decl,
        special_file_types=special_files or [],
        block_rules=block_rules or [],
        llm_call_prompt=llm_prompt,
        skip_tokens=skip_tokens or [],
        supports_bare_identifiers=bare_ids,
        bare_id_negative_lookahead=bare_lookahead,
        call_keyword_patterns=call_kw_patterns or [],
        single_line_comment=single_comment,
        multi_line_comment_open=ml_open,
        multi_line_comment_close=ml_close,
        string_delimiters=string_delims or ['"'],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Seed profiles — one per language
# ═══════════════════════════════════════════════════════════════════════════

_DELPHI = _p(
    "delphi",
    aliases=["pascal", "object_pascal"],
    extensions=[".pas", ".dpr", ".dfm", ".inc"],
    func_pats=[
        r"^\s*(?:class[ \t]+)?(?:function|procedure)[ \t]+(?:(?P<class_prefix>\w+)\.)?(?P<name>\w+)",
        r"^\s*(?:constructor|destructor)[ \t]+(?:(?P<class_prefix>\w+)\.)?(?P<name>\w+)",
    ],
    named_groups={"name": "name", "class_prefix": "class_prefix"},
    fwd_decl=ForwardDeclarationConfig(
        keyword="implementation",
        strategy="discard_before_keyword_unless_class_prefix",
    ),
    special_files=[
        SpecialFileConfig(
            extension=".dfm",
            parser_type="dfm_form",
            object_pattern=r"^\s*object\s+(?P<name>\w+)\s*:\s*(?P<class>\w+)",
            event_pattern=r"^\s*On\w+\s*=\s*(?P<handler>\w+)",
            metadata_keys=["event_handlers", "is_dfm_form"],
        ),
    ],
    llm_prompt="""\

DELPHI / OBJECT PASCAL — language-specific rules:

CALL PATTERNS (report these):
- Parameterless procedure statements: `InitialiseEcran;` — no parentheses, this IS a call.
- Parameterless function used as expression or argument:
    `result := GetCheminVersLesDocuments;`
    `tpath.combine(GetCheminVersLesDocuments, 'DB')`
  The bare identifier IS a function call even without `()`.
- Procedure/function with arguments: `TraitementDeLaBase(Edit1.Text, Table);`
- Method calls on objects: `DataModule2.LoadData;` or `DataModule2.LoadData(x);`
  — if `LoadData` is in KNOWN FUNCTIONS, report it.
- `inherited` followed by a known name: `inherited Create;` — report `Create`.
- Calls inside `with` blocks still count.

NOT CALLS (do NOT report):
- `var Form1: TForm1;` — variable declaration, not a call.
- `uses Unit3, SysUtils;` — unit import, not a call.
- `TForm1 = class(TForm)` — type declaration, not a call.
- Property access that is NOT in KNOWN FUNCTIONS: `Edit1.Text`, `Sender.Tag`.
- The `T`-prefixed class name in the method header: `procedure TForm1.Button1Click` — do not report `TForm1`.
- Forward declarations in the `interface` section.
""",
    skip_tokens=[
        "BEGIN", "END", "VAR", "CONST", "TYPE", "USES", "UNIT", "INTERFACE",
        "IMPLEMENTATION", "PROGRAM", "PROCEDURE", "FUNCTION", "CONSTRUCTOR",
        "DESTRUCTOR", "PROPERTY", "INHERITED", "RESULT", "NIL", "THEN", "DO",
        "OF", "TO", "DOWNTO", "REPEAT", "UNTIL", "CASE", "WITH", "TRY",
        "FINALLY", "EXCEPT", "RAISE", "EXIT", "BREAK", "CONTINUE", "IF",
        "ELSE", "FOR", "WHILE", "NOT", "AND", "OR", "IN", "IS", "AS",
        "CLASS", "RECORD", "OBJECT", "SET", "FILE", "ARRAY",
        "STRING", "INTEGER", "BOOLEAN", "TRUE", "FALSE", "SELF",
    ],
    bare_ids=True,
    bare_lookahead=r"(?!\s*\.)",
    single_comment="//",
    ml_open="{",
    ml_close="}",
    string_delims=["'"],
)

_PYTHON = _p(
    "python",
    extensions=[".py"],
    func_pats=[
        r"^(?P<indent>[ \t]*)(?:async\s+)?def\s+(?P<name>\w+)\s*\(",
    ],
    class_pats=[
        r"^(?P<indent>[ \t]*)class\s+(?P<name>\w+)",
    ],
    llm_prompt="""\

PYTHON — language-specific rules:

CALL PATTERNS (report these):
- Direct calls: `foo()`, `foo(arg1, arg2)`
- Method calls if the method name is in KNOWN FUNCTIONS: `obj.process_data()`
- `super().__init__()` or `super().method()` — report `method` if in KNOWN FUNCTIONS.
- Calls used as arguments: `print(compute_value())` — report `compute_value`.
- Calls in comprehensions/generators: `[transform(x) for x in items]` — report `transform`.
- Decorator calls that invoke known functions: `@retry(max=3)` — report `retry` only if in KNOWN FUNCTIONS.

NOT CALLS (do NOT report):
- `import module` or `from module import name` — imports, not calls.
- `@decorator` without parentheses used only as decoration syntax.
- Class definitions: `class Foo(Base):` — do not report `Base`.
- Type hints: `x: List[int]` — not a call.
""",
    single_comment="#",
    ml_open='"""',
    ml_close='"""',
    string_delims=['"', "'"],
)

_JAVA = _p(
    "java",
    extensions=[".java"],
    func_pats=[
        r"(?:(?:public|private|protected|static|final|abstract|synchronized)\s+)*"
        r"[\w<>\[\],\s]+\s+(?P<name>\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{",
    ],
    class_pats=[
        r"class\s+(?P<name>\w+)",
    ],
    llm_prompt="""\

JAVA — language-specific rules:

CALL PATTERNS (report these):
- Direct calls: `processData(args)`
- Static calls: `ClassName.staticMethod()` — report `staticMethod` if in KNOWN FUNCTIONS.
- Constructor: `new ClassName(args)` — report `ClassName` if in KNOWN FUNCTIONS.
- Chained calls: `obj.prepare().execute()` — report each method in KNOWN FUNCTIONS.
- `super.method()` or `this.method()` — report `method`.

NOT CALLS (do NOT report):
- `import com.example.Foo;` — import, not a call.
- Annotations: `@Override`, `@Autowired` — not calls.
- Type declarations: `List<String> items` — not a call.
- Class/interface declarations: `class Foo extends Bar` — do not report `Bar`.
""",
)

_JAVASCRIPT = _p(
    "javascript",
    extensions=[".js", ".jsx"],
    func_pats=[
        r"(?:async\s+)?function\s+(?P<name>\w+)\s*\(",
        r"(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s+)?\(",
        r"(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s+)?function",
    ],
    class_pats=[
        r"class\s+(?P<name>\w+)",
    ],
    llm_prompt="""\

JAVASCRIPT — language-specific rules:

CALL PATTERNS (report these):
- Direct calls: `processData(args)`, `fetchItems()`
- Method calls: `obj.method()` — report `method` if in KNOWN FUNCTIONS.
- Callbacks passed by name: `array.map(transformItem)` — report `transformItem` if in KNOWN FUNCTIONS.
- IIFE patterns: `(function init() { ... })()` — report `init` if in KNOWN FUNCTIONS.
- `await asyncFunction()` — report `asyncFunction`.

NOT CALLS (do NOT report):
- `require('module')` or `import ... from 'module'` — module imports, not project calls.
- `export default function` — declaration, not a call.
- `new Promise(resolve => ...)` — built-in, not a project call.
""",
)

_TYPESCRIPT = _p(
    "typescript",
    extensions=[".ts", ".tsx"],
    func_pats=[
        r"(?:async\s+)?function\s+(?P<name>\w+)\s*[\(<]",
        r"(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s+)?\(",
    ],
    class_pats=[
        r"(?:export\s+)?class\s+(?P<name>\w+)",
        r"(?:export\s+)?interface\s+(?P<name>\w+)",
    ],
    llm_prompt="""\

TYPESCRIPT — language-specific rules:

CALL PATTERNS (report these):
- Direct calls: `processData(args)`, `fetchItems()`
- Method calls: `obj.method()` — report `method` if in KNOWN FUNCTIONS.
- Callbacks passed by name: `array.map(transformItem)` — report `transformItem` if in KNOWN FUNCTIONS.
- `await asyncFunction()` — report `asyncFunction`.
- Generic calls: `createInstance<T>(factory)` — report `createInstance`.

NOT CALLS (do NOT report):
- `import { Foo } from './module'` — import, not a call.
- Type annotations: `x: SomeType`, `as SomeType` — not calls.
- Interface/type declarations: `interface Foo extends Bar` — not a call.
- `export default function` — declaration, not a call.
""",
)

_CSHARP = _p(
    "csharp",
    extensions=[".cs"],
    func_pats=[
        r"(?:(?:public|private|protected|internal|static|virtual|override|abstract|async)\s+)*"
        r"[\w<>\[\]]+\s+(?P<name>\w+)\s*\(",
    ],
    class_pats=[
        r"class\s+(?P<name>\w+)",
    ],
    llm_prompt="""\

C# — language-specific rules:

CALL PATTERNS (report these):
- Direct calls: `ProcessData(args)`
- Static calls: `ClassName.StaticMethod()` — report `StaticMethod` if in KNOWN FUNCTIONS.
- Constructor: `new ClassName(args)` — report `ClassName` if in KNOWN FUNCTIONS.
- `base.Method()` — report `Method`.
- Delegate invocations if the delegate name is in KNOWN FUNCTIONS.

NOT CALLS (do NOT report):
- `using System.Linq;` — import, not a call.
- Attributes: `[Serializable]`, `[HttpGet]` — not calls.
- Type declarations, inheritance: `class Foo : Bar` — not a call.
- Property declarations: `public string Name { get; set; }` — not a call.
""",
)

_GO = _p(
    "go",
    extensions=[".go"],
    func_pats=[
        r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(?P<name>\w+)\s*\(",
    ],
)

_KOTLIN = _p(
    "kotlin",
    extensions=[".kt"],
    func_pats=[
        r"(?:suspend\s+)?fun\s+(?P<name>\w+)\s*[\(<]",
    ],
    class_pats=[
        r"class\s+(?P<name>\w+)",
    ],
)

_COBOL = _p(
    "cobol",
    extensions=[".cbl", ".cob"],
    func_pats=[
        r"^\s*(?P<name>[A-Z0-9\-]+)\s+(?:SECTION|DIVISION)\.",
        r"^\s*(?P<name>[A-Z0-9\-]+)\.\s*$",
    ],
    llm_prompt="""\

COBOL — language-specific rules:

CALL PATTERNS (report these):
- `PERFORM paragraph-name` or `PERFORM section-name` — report the paragraph/section if in KNOWN FUNCTIONS.
- `PERFORM paragraph-name THRU paragraph-name-exit` — report the first paragraph.
- `CALL 'program-name'` or `CALL program-name` — report the program name if in KNOWN FUNCTIONS.
- `CALL variable USING ...` — if the variable value resolves to a known function, report it.

NOT CALLS (do NOT report):
- `COPY copybook-name` — include directive, not a call.
- `MOVE`, `ADD`, `COMPUTE` — data manipulation verbs, not calls.
- Section/paragraph headers (definitions): `MY-PARAGRAPH.` — declaration, not a call.
""",
    single_comment="*>",
    ml_open="",
    ml_close="",
)

_RPG = _p(
    "rpg",
    aliases=["rpgle"],
    extensions=[".rpg", ".rpgle"],
    func_pats=[
        r"^\s*DCL-PROC\s+(?P<name>\w+)",
        r"^\s*BEGSR\s+(?P<name>\w+)",
    ],
    block_rules=[
        BlockRuleConfig(
            block_type="procedure",
            open_pattern=r"^\s*DCL-PROC\s+(?P<name>\w+)",
            close_pattern=r"^\s*END-PROC\b[^;\n]*;?",
        ),
        BlockRuleConfig(
            block_type="subroutine",
            open_pattern=r"^\s*BEGSR\s+(?P<name>\w+)",
            close_pattern=r"^\s*ENDSR\b[^;\n]*;?",
        ),
        BlockRuleConfig(
            block_type="data_structure",
            open_pattern=r"^\s*DCL-DS\s+(?P<name>\w+)",
            close_pattern=r"^\s*END-DS\b[^;\n]*;?",
        ),
        BlockRuleConfig(
            block_type="interface",
            open_pattern=r"^\s*DCL-PI\s+(?P<name>\w+|\*N)",
            close_pattern=r"^\s*END-PI\b[^;\n]*;?",
        ),
    ],
    llm_prompt="""\

RPG / RPGLE — language-specific rules:

CALL PATTERNS (report these):
- `CALLP procedureName(args)` — report `procedureName`.
- `EXSR subroutineName` — report the subroutine if in KNOWN FUNCTIONS.
- `procedureName(args)` in free-format RPG — report `procedureName`.
- `CALL 'PROGRAMNAME'` — report if in KNOWN FUNCTIONS.

NOT CALLS (do NOT report):
- `/COPY` or `/INCLUDE` — preprocessor directives, not calls.
- `DCL-S`, `DCL-DS`, `DCL-PR` — declarations, not calls.
- `BEGSR subroutineName` — subroutine definition header, not a call.
""",
)

_NATURAL = _p(
    "natural",
    extensions=[".nat", ""],
    func_pats=[
        r"^(?:\d+\s+)?\s*DEFINE\s+(?:SUBROUTINE|FUNCTION)\s+(?P<name>\w[\w\-]*)",
        r"^1NEXT\s+L\s+(?P<name>\w[\w\-]*)",
    ],
    block_rules=[
        BlockRuleConfig(
            block_type="subroutine",
            open_pattern=r"^(?:\d+\s+)?\s*DEFINE\s+SUBROUTINE\s+(?P<name>\w[\w\-]*)",
            close_pattern=r"^(?:\d+\s+)?\s*END-SUBROUTINE\b",
        ),
        BlockRuleConfig(
            block_type="function",
            open_pattern=r"^(?:\d+\s+)?\s*DEFINE\s+FUNCTION\s+(?P<name>\w[\w\-]*)",
            close_pattern=r"^(?:\d+\s+)?\s*END-FUNCTION\b",
        ),
        BlockRuleConfig(
            block_type="class",
            open_pattern=r"^(?:\d+\s+)?\s*DEFINE\s+CLASS\s+(?P<name>\w[\w\-]*)",
            close_pattern=r"^(?:\d+\s+)?\s*END-CLASS\b",
        ),
    ],
    llm_prompt="""
NATURAL / ADABAS language specifics:

CALL PATTERNS (report these):
- `FETCH 'ProgramName'` or `FETCH 'ProgramName' parameters` — inter-program call.
- `FETCH RETURN 'ProgramName'` — call with return to caller.
- `CALLNAT 'SubprogramName' parameters` — call to a Natural subprogram.
- `PERFORM SubroutineName` — call to a local subroutine (DEFINE SUBROUTINE).

NOT CALLS (do NOT report):
- `INPUT USING MAP 'MapName'` — screen layout reference, NOT a program call.
- `#VARIABLE = 'ProgramName'` — data assignment, NOT a call, even if the string
  value matches a known program name.
- `DEFINE DATA`, `END-DEFINE`, `MOVE`, `ASSIGN`, `RESET`, `IF`, `FOR`, `READ`,
  `HISTOGRAM`, `FIND`, `LOOP`, `END-READ`, `ESCAPE`, `WRITE`, `REDEFINE`,
  `COMPRESS`, `FORMAT`, `SET KEY`, `EJECT`, `SKIP`, `DIVIDE` — statements, not calls.
- The program's own name on the header line (`1NEXT L  ProgramName`) is NOT a call.
- Program names in FETCH/CALLNAT are enclosed in single quotes.
""",
    skip_tokens=[
        "DEFINE", "END-DEFINE", "MOVE", "ASSIGN", "RESET", "IF", "THEN",
        "ELSE", "FOR", "END-FOR", "READ", "END-READ", "HISTOGRAM", "FIND",
        "LOOP", "ESCAPE", "WRITE", "INPUT", "REDEFINE", "COMPRESS", "FORMAT",
        "SET", "EJECT", "SKIP", "DIVIDE", "DO", "DOEND", "END", "END-IF",
        "IGNORE", "MARK", "SOUND", "ALARM", "CONST", "VIEW",
    ],
    bare_ids=True,
    call_kw_patterns=[
        r"(?:FETCH|FETCH\s+RETURN)\s+'(?P<callee>\w+)'",
        r"CALLNAT\s+'(?P<callee>\w+)'",
        r"PERFORM\s+(?P<callee>\w[\w\-]*)",
    ],
    single_comment="/*",
    ml_open="/*",
    ml_close="*/",
    string_delims=["'"],
)

_FOCUS = _p(
    "focus",
    extensions=[".foc"],
    func_pats=[
        r"^-\s*DEFINE\s+(?:FUNCTION|FILE)\s+(?P<name>\w+)",
    ],
    block_rules=[
        BlockRuleConfig(
            block_type="procedure",
            open_pattern=r"^-\s*DEFINE\s+(?:FUNCTION|FILE)\s+(?P<name>\w+)",
            close_pattern=r"^-\s*END\b",
        ),
        BlockRuleConfig(
            block_type="table_request",
            open_pattern=r"^\s*TABLE\s+FILE\s+(?P<name>\w+)",
            close_pattern=r"^\s*END\b",
        ),
        BlockRuleConfig(
            block_type="graph",
            open_pattern=r"^\s*GRAPH\s+FILE\s+(?P<name>\w+)",
            close_pattern=r"^\s*END\b",
        ),
        BlockRuleConfig(
            block_type="if_block",
            open_pattern=r"^-\s*IF\s+(?P<name>.+)",
            close_pattern=r"^-\s*ENDIF\b",
        ),
    ],
)


# ── Registry ────────────────────────────────────────────────────────────────

_SEED_PROFILES: dict[str, LanguageProfile] = {
    "delphi": _DELPHI,
    "pascal": _DELPHI,
    "python": _PYTHON,
    "java": _JAVA,
    "javascript": _JAVASCRIPT,
    "typescript": _TYPESCRIPT,
    "csharp": _CSHARP,
    "go": _GO,
    "kotlin": _KOTLIN,
    "cobol": _COBOL,
    "rpg": _RPG,
    "rpgle": _RPG,
    "natural": _NATURAL,
    "focus": _FOCUS,
}


def get_seed_profile(language: str) -> LanguageProfile | None:
    """Return a deep copy of the seed profile for *language*, or None."""
    seed = _SEED_PROFILES.get(language.lower())
    if seed is None:
        return None
    return seed.model_copy(deep=True)


def get_all_seed_profiles() -> dict[str, LanguageProfile]:
    """Return copies of all seed profiles keyed by canonical language name."""
    return {lang: p.model_copy(deep=True) for lang, p in _SEED_PROFILES.items()}
