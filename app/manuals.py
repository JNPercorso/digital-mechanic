"""Реестр руководств: какие документы относятся к какому двигателю.

Агент ищет и ссылается только на документы двигателя выбранной машины (F4 ТЗ):
норматив от другого двигателя считается ошибкой.
"""
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUALS_DIR = ROOT / "manuals"


@dataclass(frozen=True)
class Manual:
    doc_id: str      # внутренний идентификатор
    file: str        # имя PDF в папке manuals/
    title: str       # обозначение/название документа для ссылки и заявки (раздел 3 ТЗ)
    kind: str        # "РЭ" эксплуатация, "РК" ремонт, "РЭиР" всё вместе
    engines: tuple   # модели двигателей из vehicles.json


MANUALS = [
    Manual("yamz236_re", "yamz-236_re_2010.pdf", "236-3902150-Б РЭ", "РЭ", ("ЯМЗ-236М2",)),
    Manual("yamz236_rk", "yamz-236_rk_2014.pdf", "236.01.01 РК", "РК", ("ЯМЗ-236М2",)),
    Manual("yamz536_re", "yamz-536_re_2016.pdf", "536.3902150 РЭ", "РЭ", ("ЯМЗ-536",)),
    Manual("yamz536_rk", "yamz-536_rk_2013.pdf", "536.01.02 РК", "РК", ("ЯМЗ-536",)),
    Manual("zmz5234_rr", "zmz-5234_rk_2025.pdf",
           "Двигатели ЗМЗ-5234.10 со сцеплением. Руководство по ремонту", "РК", ("ЗМЗ-5234.10",)),
    Manual("zmz409_re", "zmz-40904-40905_rk_2025.pdf",
           "Руководство по эксплуатации, техническому обслуживанию и ремонту двигателей ЗМЗ-40904.10 и ЗМЗ-40905.10",
           "РЭиР", ("ЗМЗ-40904.10", "ЗМЗ-40905.10")),
]

BY_ID = {m.doc_id: m for m in MANUALS}


def manuals_for_engine(engine_model: str) -> list[Manual]:
    """Документы для модели двигателя; пустой список, если двигатель неизвестен."""
    return [m for m in MANUALS if engine_model in m.engines]
