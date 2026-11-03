"""Custom 1C:Enterprise (EDT) Parsers for knowledge-rag.

Supports:
1. 1C BSL code modules (.bsl) with intelligent method chunking, region tracking,
   query cleaning, query table extraction, and structured context headers.
2. 1C EDT metadata files (.mdo) with XML parsing, dimension/resource/attribute/tooltip
   extraction, and structured Markdown representation.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Mapping of EDT source directory names to Russian metadata types
EDT_METADATA_DIRS: Dict[str, str] = {
    "accumulationregisters": "РегистрНакопления",
    "accountingregisters": "РегистрБухгалтерии",
    "calculationregisters": "РегистрРасчета",
    "informationregisters": "РегистрСведений",
    "catalogs": "Справочник",
    "documents": "Документ",
    "documentjournals": "ЖурналДокументов",
    "enums": "Перечисление",
    "reports": "Отчет",
    "dataprocessors": "Обработка",
    "externaldataprocessors": "ВнешняяОбработка",
    "externalreports": "ВнешнийОтчет",
    "chartsofcharacteristictypes": "ПланВидовХарактеристик",
    "chartsofaccounts": "ПланСчетов",
    "chartsofcalculationtypes": "ПланВидовРасчета",
    "businessprocesses": "БизнесПроцесс",
    "tasks": "Задача",
    "exchangeplans": "ПланОбмена",
    "commonmodules": "ОбщийМодуль",
    "commoncommands": "ОбщаяКоманда",
    "commonforms": "ОбщаяФорма",
    "constants": "Константа",
    "definedtypes": "ОпределяемыйТип",
    "eventsubscriptions": "ПодпискаНаСобытие",
    "scheduledjobs": "РегламентноеЗадание",
    "functionaloptions": "ФункциональнаяОпция",
    "settingsstorages": "ХранилищеНастроек",
    "subsystems": "Подсистема",
    "xdtopackages": "ПакетXDTO",
    "webservices": "WebСервис",
    "httpservices": "HTTPСервис",
}

# Mapping of BSL module file names to module types
EDT_MODULE_FILES: Dict[str, str] = {
    "objectmodule.bsl": "МодульОбъекта",
    "managermodule.bsl": "МодульМенеджера",
    "recordsetmodule.bsl": "МодульНабораЗаписей",
    "commandmodule.bsl": "МодульКоманды",
    "valuemanagermodule.bsl": "МодульМенеджераЗначения",
    "managedapplicationmodule.bsl": "МодульУправляемогоПриложения",
    "ordinaryapplicationmodule.bsl": "МодульОбычногоПриложения",
    "sessionmodule.bsl": "МодульСеанса",
    "externalconnectionmodule.bsl": "МодульВнешнегоСоединения",
}

# XML namespace for EDT mdclass
XML_NAMESPACES = {
    "mdclass": "http://g5.1c.ru/v8/dt/metadata/mdclass",
    "core": "http://g5.1c.ru/v8/dt/mcore",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

# Regex patterns for BSL parsing
REGION_START_RE = re.compile(r"^[ \t]*#[Оо]бласть\s+(.+)$")
REGION_END_RE = re.compile(r"^[ \t]*#[Кк]онец[Оо]бласти", re.IGNORECASE)
METHOD_START_RE = re.compile(
    r"^[ \t]*(?:(Асинх|Async)[ \t]+)?(Процедура|Функция|Procedure|Function)[ \t]+([A-Za-zА-Яа-яёЁ0-9_]+)[ \t]*\((.*?)\)(?:[ \t]+(Экспорт|Export))?",
    re.IGNORECASE,
)
METHOD_END_RE = re.compile(r"^[ \t]*(КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\b", re.IGNORECASE)
COMPILATION_DIRECTIVE_RE = re.compile(r"^[ \t]*&([A-Za-zА-Яа-яёЁ0-9_]+)")
QUERY_LINE_PREFIX_RE = re.compile(r"^[ \t]*\|[ \t]?")
QUERY_TABLES_RE = re.compile(
    r"(?i)\b(?:ИЗ|СОЕДИНЕНИЕ|FROM|JOIN)\s+([A-Za-zА-Яа-яёЁ0-9_]+(?:\.[A-Za-zА-Яа-яёЁ0-9_]+)+)"
)


def detect_1c_project(filepath: Path) -> str:
    """Detect high-level project name from file path."""
    path_str = str(filepath).replace("\\", "/").lower()
    if "git/tests" in path_str or "tests/тесты" in path_str:
        return "ТестыASPU"
    if "pif3" in path_str:
        return "PIF3"
    if "ssl303" in path_str:
        return "SSL303"
    if "бсп_3_1_11_115" in path_str or "bsp31" in path_str:
        return "SSL3111"
    if "s3_ssl" in path_str:
        return "S3_SSL"
    if "внешнииобработки" in path_str:
        return "ВнешниеОбработки"
    if "git" in path_str:
        return "ASPU"
    return "Общий"


def resolve_1c_object_context(filepath: Path) -> Tuple[str, str, str]:
    """Resolve 1C metadata object name, module type, and unified header context.

    Returns:
        (object_name, module_type, display_title)
        e.g. ("Документ.РасчетДанныхДляВнешнихСистем", "МодульМенеджера",
              "Документ.РасчетДанныхДляВнешнихСистем (МодульМенеджера)")
    """
    parts = list(filepath.parts)
    parts_lower = [p.lower() for p in parts]
    filename_lower = filepath.name.lower()

    # Search for EDT metadata folder in path parts
    for idx, part_lower in enumerate(parts_lower):
        if part_lower in EDT_METADATA_DIRS:
            meta_type_ru = EDT_METADATA_DIRS[part_lower]
            if idx + 1 < len(parts):
                meta_name = parts[idx + 1]
                object_name = f"{meta_type_ru}.{meta_name}"

                # CommonModule case
                if meta_type_ru == "ОбщийМодуль":
                    return object_name, "ОбщийМодуль", object_name

                # Check if this is a Form module
                # e.g., .../Forms/<FormName>/Module.bsl
                if "forms" in parts_lower[idx + 2 :]:
                    forms_idx = idx + 2 + parts_lower[idx + 2 :].index("forms")
                    if forms_idx + 1 < len(parts):
                        form_name = parts[forms_idx + 1]
                        full_name = f"{object_name}.Форма.{form_name}"
                        return full_name, "МодульФормы", f"{full_name} (МодульФормы)"

                # Check if standard object / manager / recordset module
                if filename_lower in EDT_MODULE_FILES:
                    mod_type = EDT_MODULE_FILES[filename_lower]
                    return object_name, mod_type, f"{object_name} ({mod_type})"

                # MDO metadata definition file
                if filename_lower.endswith(".mdo"):
                    return object_name, "Метаданные", f"{object_name} (Метаданные)"

                # Generic module under object
                return object_name, filepath.stem, f"{object_name} ({filepath.stem})"

    # Fallback for scripts, tests or root files
    if filename_lower in EDT_MODULE_FILES:
        mod_type = EDT_MODULE_FILES[filename_lower]
        return filepath.parent.name, mod_type, f"{filepath.parent.name} ({mod_type})"

    stem = filepath.stem
    return stem, "Модуль", stem


def clean_query_text(text: str) -> str:
    """Normalize multi-line 1C query string literals by stripping leading '|' markers."""
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        cleaned_lines.append(QUERY_LINE_PREFIX_RE.sub("", line))
    return "\n".join(cleaned_lines)


def extract_query_tables(text: str) -> List[str]:
    """Extract accessed 1C metadata tables from SQL/1C query text.

    Examples:
        РегистрНакопления.ОстаткиПаев.Остатки -> captures
        Справочник.ЛицевыеСчета -> captures
        РегистрСведений.СтатусыОбмена.СрезПоследних -> captures
    """
    matches = QUERY_TABLES_RE.findall(text)
    # Deduplicate preserving order
    seen = set()
    tables = []
    for m in matches:
        # Ignore parameter markers or single token aliases
        clean_name = m.strip()
        if clean_name.startswith("&") or "." not in clean_name:
            continue
        if clean_name.lower() not in seen:
            seen.add(clean_name.lower())
            tables.append(clean_name)
    return tables


@dataclass
class BslMethod:
    """Parsed BSL procedure or function."""

    name: str
    is_function: bool
    is_export: bool
    is_async: bool
    region: str
    preamble: str  # Comments, annotations (@strict-types), directives (&НаСервере)
    signature: str
    body: str
    start_line: int
    end_line: int
    query_tables: List[str] = field(default_factory=list)

    @property
    def full_source(self) -> str:
        parts = []
        if self.preamble:
            parts.append(self.preamble)
        parts.append(self.signature)
        if self.body:
            parts.append(self.body)
        end_keyword = "КонецФункции" if self.is_function else "КонецПроцедуры"
        parts.append(end_keyword)
        return "\n".join(parts)


def parse_bsl_methods(content: str) -> Tuple[List[str], List[BslMethod], List[str]]:
    """Parse BSL module lines into module-header lines, list of BslMethods, and module-footer lines."""
    lines = content.split("\n")
    region_stack: List[str] = []
    methods: List[BslMethod] = []

    header_lines: List[str] = []
    footer_lines: List[str] = []

    current_preamble: List[str] = []
    in_method = False
    current_method_name = ""
    current_method_is_fn = False
    current_method_is_export = False
    current_method_is_async = False
    current_method_sig = ""
    current_method_body: List[str] = []
    current_method_start_line = 0

    first_method_encountered = False

    for line_idx, line in enumerate(lines):
        stripped = line.strip()

        # Track region directives
        reg_start = REGION_START_RE.match(line)
        if reg_start:
            region_stack.append(reg_start.group(1).strip())
            if not in_method:
                current_preamble.append(line)
            else:
                current_method_body.append(line)
            continue

        if REGION_END_RE.match(line):
            if region_stack:
                region_stack.pop()
            if not in_method:
                current_preamble.append(line)
            else:
                current_method_body.append(line)
            continue

        # Check method start
        method_match = METHOD_START_RE.match(line)
        if method_match and not in_method:
            first_method_encountered = True
            in_method = True
            is_async = bool(method_match.group(1))
            is_fn = method_match.group(2).lower() in ("функция", "function")
            m_name = method_match.group(3)
            is_export = bool(method_match.group(5))

            current_method_name = m_name
            current_method_is_fn = is_fn
            current_method_is_export = is_export
            current_method_is_async = is_async
            current_method_sig = line
            current_method_body = []
            current_method_start_line = line_idx + 1
            continue

        # Check method end
        if in_method and METHOD_END_RE.match(line):
            in_method = False
            cur_region = region_stack[-1] if region_stack else "Основная"
            preamble_text = "\n".join(current_preamble).strip()
            body_text = "\n".join(current_method_body)

            # Analyze query tables
            full_text = f"{current_method_sig}\n{body_text}"
            q_tables = extract_query_tables(clean_query_text(full_text))

            method_obj = BslMethod(
                name=current_method_name,
                is_function=current_method_is_fn,
                is_export=current_method_is_export,
                is_async=current_method_is_async,
                region=cur_region,
                preamble=preamble_text,
                signature=current_method_sig,
                body=body_text,
                start_line=current_method_start_line,
                end_line=line_idx + 1,
                query_tables=q_tables,
            )
            methods.append(method_obj)
            current_preamble = []
            continue

        if in_method:
            current_method_body.append(line)
        else:
            if not first_method_encountered:
                # Accumulate module-level header or preamble for first method
                # If line is comment or directive, hold in preamble
                if (
                    stripped.startswith("//")
                    or stripped.startswith("&")
                    or stripped.startswith("#Если")
                    or stripped.startswith("#КонецЕсли")
                    or stripped.startswith("#Иначе")
                ):
                    current_preamble.append(line)
                elif stripped:
                    # Variable declaration or other code before first method
                    header_lines.extend(current_preamble)
                    current_preamble = []
                    header_lines.append(line)
            else:
                # Between methods or after last method
                if (
                    stripped.startswith("//")
                    or stripped.startswith("&")
                    or stripped.startswith("#Если")
                    or stripped.startswith("#КонецЕсли")
                    or stripped.startswith("#Иначе")
                ):
                    current_preamble.append(line)
                elif stripped:
                    footer_lines.extend(current_preamble)
                    current_preamble = []
                    footer_lines.append(line)

    if current_preamble:
        footer_lines.extend(current_preamble)

    return header_lines, methods, footer_lines


def build_bsl_chunks(
    filepath: Path,
    content: str,
    metadata: Dict[str, Any],
    chunk_class: Any,
    chunk_size: int = 2500,
) -> List[Any]:
    """Chunk BSL module code into semantically rich, method-bounded Chunks."""
    project = detect_1c_project(filepath)
    object_name, module_type, display_title = resolve_1c_object_context(filepath)
    display_title = f"[{project}] {display_title}"
    header_lines, methods, footer_lines = parse_bsl_methods(content)

    chunks: List[Any] = []
    global_index = 0
    char_offset = 0

    # 1. Header chunk (module variables / preprocessor settings if meaningful)
    header_text = "\n".join(header_lines).strip()
    if header_text and len(header_text) > 30:
        clean_header = clean_query_text(header_text)
        header_content = (
            f"// [1С BSL] Проект: {project} | Объект: {object_name} ({module_type})\n"
            f"// Раздел: Переменные модуля и директивы\n"
            f"//\n"
            f"{clean_header}"
        )
        c = chunk_class(
            content=header_content,
            index=global_index,
            start_char=char_offset,
            end_char=char_offset + len(header_text),
            metadata={
                "title": display_title,
                "project": project,
                "type": "bsl_code",
                "object_name": object_name,
                "module_type": module_type,
                "method_name": "ПеременныеМодуля",
                "section": "ШапкаМодуля",
            },
        )
        chunks.append(c)
        global_index += 1
        char_offset += len(header_text)

    # 2. Methods
    for method in methods:
        method_source = method.full_source
        cleaned_source = clean_query_text(method_source)
        q_tables_str = ", ".join(method.query_tables) if method.query_tables else ""

        # Context header
        export_label = " (Экспорт)" if method.is_export else ""
        query_header_line = f"// Запросы к таблицам: {q_tables_str}\n" if q_tables_str else ""
        base_header = (
            f"// [1С BSL] Проект: {project} | Объект: {object_name} ({module_type})\n"
            f"// Область: {method.region} | Метод: {method.name}{export_label}\n"
            f"{query_header_line}//\n"
        )

        full_chunk_text = f"{base_header}{cleaned_source}"

        if len(full_chunk_text) <= chunk_size:
            meta = {
                "title": display_title,
                "project": project,
                "type": "bsl_code",
                "object_name": object_name,
                "module_type": module_type,
                "region": method.region,
                "method_name": method.name,
                "is_export": method.is_export,
            }
            if q_tables_str:
                meta["query_tables"] = q_tables_str

            c = chunk_class(
                content=full_chunk_text,
                index=global_index,
                start_char=char_offset,
                end_char=char_offset + len(method_source),
                metadata=meta,
            )
            chunks.append(c)
            global_index += 1
        else:
            # Sub-chunking long method
            body_lines = cleaned_source.split("\n")
            sub_chunks_lines: List[List[str]] = []
            current_sub: List[str] = []
            cur_len = len(base_header)

            for bline in body_lines:
                if cur_len + len(bline) + 1 > chunk_size and current_sub:
                    sub_chunks_lines.append(current_sub)
                    current_sub = [bline]
                    cur_len = len(base_header) + len(bline) + 1
                else:
                    current_sub.append(bline)
                    cur_len += len(bline) + 1
            if current_sub:
                sub_chunks_lines.append(current_sub)

            total_subs = len(sub_chunks_lines)
            for sub_idx, sub_lines in enumerate(sub_chunks_lines, 1):
                sub_header = (
                    f"// [1С BSL] Проект: {project} | Объект: {object_name} ({module_type})\n"
                    f"// Область: {method.region} | Метод: {method.name}{export_label} [Часть {sub_idx}/{total_subs}]\n"
                    f"{query_header_line}//\n"
                )
                sub_text = f"{sub_header}" + "\n".join(sub_lines)
                meta = {
                    "title": display_title,
                    "project": project,
                    "type": "bsl_code",
                    "object_name": object_name,
                    "module_type": module_type,
                    "region": method.region,
                    "method_name": method.name,
                    "is_export": method.is_export,
                    "part": f"{sub_idx}/{total_subs}",
                }
                if q_tables_str:
                    meta["query_tables"] = q_tables_str

                c = chunk_class(
                    content=sub_text,
                    index=global_index,
                    start_char=char_offset,
                    end_char=char_offset + len(sub_text),
                    metadata=meta,
                )
                chunks.append(c)
                global_index += 1

        char_offset += len(method_source)

    # 3. Footer chunk (module initialization code if present)
    footer_text = "\n".join(footer_lines).strip()
    if footer_text and len(footer_text) > 30:
        clean_footer = clean_query_text(footer_text)
        footer_content = (
            f"// [1С BSL] Проект: {project} | Объект: {object_name} ({module_type})\n"
            f"// Раздел: Инициализация модуля\n"
            f"//\n"
            f"{clean_footer}"
        )
        c = chunk_class(
            content=footer_content,
            index=global_index,
            start_char=char_offset,
            end_char=char_offset + len(footer_text),
            metadata={
                "title": display_title,
                "project": project,
                "type": "bsl_code",
                "object_name": object_name,
                "module_type": module_type,
                "method_name": "ИнициализацияМодуля",
                "section": "Инициализация",
            },
        )
        chunks.append(c)
        global_index += 1

    return chunks


def parse_mdo_file(filepath: Path) -> Tuple[str, Dict[str, Any]]:
    """Parse 1C EDT .mdo metadata XML file into structured Markdown passport and metadata."""
    tree = ET.parse(filepath)
    root = tree.getroot()

    # Extract tag without namespace
    root_tag = root.tag
    if "}" in root_tag:
        root_tag = root_tag.split("}", 1)[1]

    meta_type_ru = EDT_METADATA_DIRS.get(root_tag.lower() + "s", root_tag)
    # Direct mappings if exact singular
    singular_map = {
        "accumulationregister": "РегистрНакопления",
        "accountingregister": "РегистрБухгалтерии",
        "calculationregister": "РегистрРасчета",
        "informationregister": "РегистрСведений",
        "catalog": "Справочник",
        "document": "Документ",
        "documentjournal": "ЖурналДокументов",
        "enum": "Перечисление",
        "report": "Отчет",
        "dataprocessor": "Обработка",
        "externaldataprocessor": "ВнешняяОбработка",
        "externalreport": "ВнешнийОтчет",
        "commonmodule": "ОбщийМодуль",
        "chartsofcharacteristictypes": "ПланВидовХарактеристик",
        "chartsofaccounts": "ПланСчетов",
        "businessprocess": "БизнесПроцесс",
        "task": "Задача",
        "exchangeplan": "ПланОбмена",
        "subsystem": "Подсистема",
        "role": "Роль",
        "scheduledjob": "РегламентноеЗадание",
    }
    meta_type_ru = singular_map.get(root_tag.lower(), meta_type_ru)

    def find_text(elem: ET.Element, path: str) -> str:
        found = elem.find(path)
        if found is not None and found.text:
            return found.text.strip()
        return ""

    def find_synonym(elem: ET.Element) -> str:
        syn = elem.find("synonym")
        if syn is not None:
            val = syn.find("value")
            if val is not None and val.text:
                return val.text.strip()
        return ""

    def find_tooltip(elem: ET.Element) -> str:
        tt = elem.find("toolTip")
        if tt is not None:
            val = tt.find("value")
            if val is not None and val.text:
                return val.text.strip()
            if tt.text and tt.text.strip():
                return tt.text.strip()
        return ""

    def find_types(elem: ET.Element) -> List[str]:
        type_elem = elem.find("type")
        if type_elem is not None:
            types_list = []
            for t in type_elem.findall("types"):
                if t.text:
                    types_list.append(t.text.strip())
            return types_list
        return []

    name = find_text(root, "name") or filepath.stem
    synonym = find_synonym(root)
    comment = find_text(root, "comment")

    full_object_name = f"{meta_type_ru}.{name}"

    # Extract dimensions, resources, attributes, tabularSections
    dimensions = []
    for d in root.findall("dimensions"):
        d_name = find_text(d, "name")
        d_syn = find_synonym(d)
        d_types = find_types(d)
        d_comm = find_text(d, "comment")
        d_tt = find_tooltip(d)
        d_deny = find_text(d, "denyIncompleteValues")
        dimensions.append(
            {
                "name": d_name,
                "synonym": d_syn,
                "types": d_types,
                "comment": d_comm,
                "tooltip": d_tt,
                "deny_incomplete": d_deny == "true",
            }
        )

    resources = []
    for r in root.findall("resources"):
        r_name = find_text(r, "name")
        r_syn = find_synonym(r)
        r_types = find_types(r)
        r_comm = find_text(r, "comment")
        r_tt = find_tooltip(r)
        resources.append(
            {
                "name": r_name,
                "synonym": r_syn,
                "types": r_types,
                "comment": r_comm,
                "tooltip": r_tt,
            }
        )

    attributes = []
    for a in root.findall("attributes"):
        a_name = find_text(a, "name")
        a_syn = find_synonym(a)
        a_types = find_types(a)
        a_comm = find_text(a, "comment")
        a_tt = find_tooltip(a)
        attributes.append(
            {
                "name": a_name,
                "synonym": a_syn,
                "types": a_types,
                "comment": a_comm,
                "tooltip": a_tt,
            }
        )

    tabular_sections = []
    for ts in root.findall("tabularSections"):
        ts_name = find_text(ts, "name")
        ts_syn = find_synonym(ts)
        ts_comm = find_text(ts, "comment")
        ts_tt = find_tooltip(ts)
        ts_attrs = []
        for a in ts.findall("attributes"):
            ts_attrs.append(
                {
                    "name": find_text(a, "name"),
                    "synonym": find_synonym(a),
                    "types": find_types(a),
                    "comment": find_text(a, "comment"),
                    "tooltip": find_tooltip(a),
                }
            )
        tabular_sections.append(
            {
                "name": ts_name,
                "synonym": ts_syn,
                "comment": ts_comm,
                "tooltip": ts_tt,
                "attributes": ts_attrs,
            }
        )

    # Build Markdown document representation
    project = detect_1c_project(filepath)
    md_lines = [
        f"# [1С Метаданные] {full_object_name} | Проект: {project}",
        f"* **Тип метаданных:** {meta_type_ru}",
        f"* **Имя:** `{name}`",
        f"* **Проект:** {project}",
    ]
    if synonym:
        md_lines.append(f"* **Синоним:** {synonym}")
    if comment:
        md_lines.append(f"* **Комментарий:** {comment}")

    # Specific flags
    extra_flags = []
    if root_tag.lower() == "accumulationregister":
        reg_type = find_text(root, "registerType")
        if reg_type:
            extra_flags.append(f"Вид регистра: {reg_type}")
    elif root_tag.lower() == "commonmodule":
        for flag in ("server", "clientManagedApplication", "privileged", "externalConnection"):
            val = find_text(root, flag)
            if val == "true":
                extra_flags.append(flag)
    if extra_flags:
        md_lines.append(f"* **Свойства:** {', '.join(extra_flags)}")

    md_lines.append("")

    if dimensions:
        md_lines.append("## Измерения")
        for d in dimensions:
            types_str = ", ".join(d["types"]) if d["types"] else "Не указан"
            flags = " *(Запрет незаполненных)*" if d["deny_incomplete"] else ""
            md_lines.append(f"- **{d['name']}** (`{types_str}`){flags}")
            if d["synonym"] and d["synonym"] != d["name"]:
                md_lines.append(f"  * Синоним: {d['synonym']}")
            if d["comment"]:
                md_lines.append(f"  * Комментарий: {d['comment']}")
            if d["tooltip"]:
                md_lines.append(f"  * Подсказка: {d['tooltip']}")
        md_lines.append("")

    if resources:
        md_lines.append("## Ресурсы")
        for r in resources:
            types_str = ", ".join(r["types"]) if r["types"] else "Не указан"
            md_lines.append(f"- **{r['name']}** (`{types_str}`)")
            if r["synonym"] and r["synonym"] != r["name"]:
                md_lines.append(f"  * Синоним: {r['synonym']}")
            if r["comment"]:
                md_lines.append(f"  * Комментарий: {r['comment']}")
            if r["tooltip"]:
                md_lines.append(f"  * Подсказка: {r['tooltip']}")
        md_lines.append("")

    if attributes:
        md_lines.append("## Реквизиты")
        for a in attributes:
            types_str = ", ".join(a["types"]) if a["types"] else "Не указан"
            md_lines.append(f"- **{a['name']}** (`{types_str}`)")
            if a["synonym"] and a["synonym"] != a["name"]:
                md_lines.append(f"  * Синоним: {a['synonym']}")
            if a["comment"]:
                md_lines.append(f"  * Комментарий: {a['comment']}")
            if a["tooltip"]:
                md_lines.append(f"  * Подсказка: {a['tooltip']}")
        md_lines.append("")

    if tabular_sections:
        md_lines.append("## Табличные части")
        for ts in tabular_sections:
            md_lines.append(f"### Табличная часть `{ts['name']}`")
            if ts["synonym"] and ts["synonym"] != ts["name"]:
                md_lines.append(f"* Синоним: {ts['synonym']}")
            if ts["comment"]:
                md_lines.append(f"* Комментарий: {ts['comment']}")
            if ts["tooltip"]:
                md_lines.append(f"* Подсказка: {ts['tooltip']}")
            for a in ts["attributes"]:
                types_str = ", ".join(a["types"]) if a["types"] else "Не указан"
                md_lines.append(f"- **{a['name']}** (`{types_str}`)")
                if a["comment"]:
                    md_lines.append(f"  * Комментарий: {a['comment']}")
                if a["tooltip"]:
                    md_lines.append(f"  * Подсказка: {a['tooltip']}")
            md_lines.append("")

    content_markdown = "\n".join(md_lines).strip()

    metadata = {
        "type": "1c_metadata",
        "project": project,
        "object_name": full_object_name,
        "meta_type": meta_type_ru,
        "name": name,
        "title": f"[{project}] {full_object_name} (Метаданные)",
        "file_size": filepath.stat().st_size,
        "modified": datetime.fromtimestamp(filepath.stat().st_mtime).isoformat(),
        "dimensions_count": len(dimensions),
        "resources_count": len(resources),
        "attributes_count": len(attributes),
        "tabular_sections_count": len(tabular_sections),
    }

    return content_markdown, metadata


def build_mdo_chunks(
    filepath: Path,
    content: str,
    metadata: Dict[str, Any],
    chunk_class: Any,
    chunk_size: int = 1800,
) -> List[Any]:
    """Chunk MDO markdown representation into section-aligned Chunks."""
    if len(content) <= chunk_size:
        return [
            chunk_class(
                content=content,
                index=0,
                start_char=0,
                end_char=len(content),
                metadata={
                    "title": metadata.get("title", filepath.stem),
                    "type": "1c_metadata",
                    "object_name": metadata.get("object_name", ""),
                    "meta_type": metadata.get("meta_type", ""),
                },
            )
        ]

    # Split by ## sections
    sections = re.split(r"(?=^##\s+)", content, flags=re.MULTILINE)
    passport_header = sections[0].strip() if sections else ""

    chunks: List[Any] = []
    global_index = 0
    char_offset = 0

    object_name = metadata.get("object_name", filepath.stem)

    for idx, sec in enumerate(sections):
        sec_stripped = sec.strip()
        if not sec_stripped:
            continue

        if idx == 0:
            # Main passport
            chunk_text = sec_stripped
        else:
            # Section chunk with passport context header
            first_line = sec_stripped.split("\n", 1)[0].replace("##", "").strip()
            chunk_header = f"# [1С Метаданные] {object_name} ({first_line})\n\n"
            chunk_text = f"{chunk_header}{sec_stripped}"

        if len(chunk_text) <= chunk_size:
            chunks.append(
                chunk_class(
                    content=chunk_text,
                    index=global_index,
                    start_char=char_offset,
                    end_char=char_offset + len(sec),
                    metadata={
                        "title": metadata.get("title", filepath.stem),
                        "type": "1c_metadata",
                        "object_name": object_name,
                        "meta_type": metadata.get("meta_type", ""),
                    },
                )
            )
            global_index += 1
        else:
            # Further split long section
            lines = sec_stripped.split("\n")
            sub_lines: List[str] = []
            cur_len = 0
            sub_idx = 1
            for line in lines:
                if cur_len + len(line) + 1 > chunk_size and sub_lines:
                    sub_text = "\n".join(sub_lines)
                    chunks.append(
                        chunk_class(
                            content=f"# [1С Метаданные] {object_name} [Часть {sub_idx}]\n\n{sub_text}",
                            index=global_index,
                            start_char=char_offset,
                            end_char=char_offset + len(sub_text),
                            metadata={
                                "title": metadata.get("title", filepath.stem),
                                "type": "1c_metadata",
                                "object_name": object_name,
                                "meta_type": metadata.get("meta_type", ""),
                            },
                        )
                    )
                    global_index += 1
                    sub_lines = [line]
                    cur_len = len(line) + 1
                    sub_idx += 1
                else:
                    sub_lines.append(line)
                    cur_len += len(line) + 1
            if sub_lines:
                sub_text = "\n".join(sub_lines)
                chunks.append(
                    chunk_class(
                        content=f"# [1С Метаданные] {object_name} [Часть {sub_idx}]\n\n{sub_text}",
                        index=global_index,
                        start_char=char_offset,
                        end_char=char_offset + len(sub_text),
                        metadata={
                            "title": metadata.get("title", filepath.stem),
                            "type": "1c_metadata",
                            "object_name": object_name,
                            "meta_type": metadata.get("meta_type", ""),
                        },
                    )
                )
                global_index += 1

        char_offset += len(sec)

    return chunks
