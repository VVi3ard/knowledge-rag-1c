"""Unit and integration tests for 1C (BSL + MDO) parsers."""

from __future__ import annotations

import unittest
from pathlib import Path
from parsers_1c import (
    clean_query_text,
    extract_query_tables,
    parse_bsl_methods,
    resolve_1c_object_context,
)


class DummyChunk:
    def __init__(self, content, index, start_char, end_char, metadata=None):
        self.content = content
        self.index = index
        self.start_char = start_char
        self.end_char = end_char
        self.metadata = metadata or {}


class Test1CParsers(unittest.TestCase):
    def test_query_cleaning(self):
        query_raw = (
            "Запрос.Текст = \n"
            "\t|\tВЫБРАТЬ\n"
            "\t|\t\tОстатки.Количество КАК Количество\n"
            "\t|\tИЗ\n"
            "\t|\t\tРегистрНакопления.ОстаткиПаев.Остатки КАК Остатки;"
        )
        cleaned = clean_query_text(query_raw)
        self.assertNotIn("|", cleaned)
        self.assertIn("ВЫБРАТЬ", cleaned)
        self.assertIn("РегистрНакопления.ОстаткиПаев.Остатки", cleaned)

    def test_query_tables_extraction(self):
        text = (
            "ВЫБРАТЬ * ИЗ РегистрНакопления.ОстаткиПаев.Остатки КАК Ост "
            "ЛЕВОЕ СОЕДИНЕНИЕ Справочник.ЛицевыеСчета КАК ЛС ПО Ост.ЛС = ЛС.Ссылка "
            "ВНУТРЕННЕЕ СОЕДИНЕНИЕ РегистрСведений.СтатусыОбмена.СрезПоследних КАК Статусы ПО 1=1"
        )
        tables = extract_query_tables(text)
        self.assertEqual(
            tables,
            [
                "РегистрНакопления.ОстаткиПаев.Остатки",
                "Справочник.ЛицевыеСчета",
                "РегистрСведений.СтатусыОбмена.СрезПоследних",
            ],
        )

    def test_bsl_method_parsing(self):
        bsl_code = """
#Область ПрограммныйИнтерфейс

// Возвращает рассчитанную сумму актива.
//
// Параметры:
//  Ссылка - ДокументСсылка.РасчетДанныхДляВнешнихСистем
//
//@strict-types
&НаСервере
Функция РассчитатьСтоимость(Ссылка) Экспорт
	Запрос = Новый Запрос(
		"ВЫБРАТЬ
		|	Сумма
		|ИЗ
		|	РегистрНакопления.ОстаткиПаев КАК ОстаткиПаев"
	);
	Возврат Запрос.Выполнить().Выгрузить();
КонецФункции

#КонецОбласти
"""
        header, methods, footer = parse_bsl_methods(bsl_code)
        self.assertEqual(len(methods), 1)
        m = methods[0]
        self.assertEqual(m.name, "РассчитатьСтоимость")
        self.assertTrue(m.is_function)
        self.assertTrue(m.is_export)
        self.assertEqual(m.region, "ПрограммныйИнтерфейс")
        self.assertIn("@strict-types", m.preamble)
        self.assertIn("&НаСервере", m.preamble)
        self.assertEqual(m.query_tables, ["РегистрНакопления.ОстаткиПаев"])

    def test_context_resolution(self):
        fake_path = Path("src/Documents/РасчетДанныхДляВнешнихСистем/ManagerModule.bsl")
        obj, mod, title = resolve_1c_object_context(fake_path)
        self.assertEqual(obj, "Документ.РасчетДанныхДляВнешнихСистем")
        self.assertEqual(mod, "МодульМенеджера")

        fake_form = Path("src/Catalogs/ЛицевыеСчета/Forms/ФормаЭлемента/Module.bsl")
        obj, mod, title = resolve_1c_object_context(fake_form)
        self.assertEqual(obj, "Справочник.ЛицевыеСчета.Форма.ФормаЭлемента")
        self.assertEqual(mod, "МодульФормы")


if __name__ == "__main__":
    unittest.main()
