from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


SCENE_TYPES = (
    "工业区",
    "闹市区",
    "商务办公区",
    "居民区",
    "高密度居民区",
    "大型停车场",
    "购物广场",
    "地铁站",
    "大型交通枢纽",
    "轻轨运营中心",
    "机场区域",
    "医院外部",
    "短波电台",
    "基站",
    "广播电视塔",
    "发电站",
    "智能网联示范区",
    "物流仓储中心",
    "公共文化区域",
    "其他",
    "未分类",
)


@dataclass(frozen=True)
class SceneLocation:
    city: str
    point: str
    scene_type: str
    classification_source: str
    notes: str
    updated_at: str
    spectrum_point: str
    iq_relative_directory: str
    iq_recording_prefix: str


@dataclass(frozen=True)
class AssociationImportResult:
    row_count: int
    location_count: int
    link_count: int
    skipped_link_count: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class IQLocationLink:
    recording_stem: str
    wsm_file: str
    ws1_file: str
    ws2_file: str


ASSOCIATION_COLUMNS = (
    "序号",
    "城市",
    "地点",
    "场景类型",
    "频谱数据目录名称",
    "IQ数据相对目录",
    "IQ数据组名称",
    "备注",
)


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS scene_locations (
            city TEXT NOT NULL,
            point TEXT NOT NULL,
            scene_type TEXT NOT NULL,
            classification_source TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            spectrum_point TEXT NOT NULL DEFAULT '',
            iq_relative_directory TEXT NOT NULL DEFAULT '',
            iq_recording_prefix TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(city, point)
        );
        CREATE INDEX IF NOT EXISTS idx_scene_locations_type
        ON scene_locations(scene_type, city, point);

        CREATE TABLE IF NOT EXISTS iq_location_links (
            city TEXT NOT NULL,
            point TEXT NOT NULL,
            recording_stem TEXT NOT NULL,
            wsm_file TEXT NOT NULL DEFAULT '',
            ws1_file TEXT NOT NULL DEFAULT '',
            ws2_file TEXT NOT NULL DEFAULT '',
            linked_at TEXT NOT NULL,
            PRIMARY KEY(city, point, recording_stem),
            FOREIGN KEY(city, point) REFERENCES scene_locations(city, point) ON DELETE CASCADE
        );
        """
    )
    location_columns = {row[1] for row in connection.execute("PRAGMA table_info(scene_locations)")}
    if "spectrum_point" not in location_columns:
        connection.execute("ALTER TABLE scene_locations ADD COLUMN spectrum_point TEXT NOT NULL DEFAULT ''")
        connection.execute("UPDATE scene_locations SET spectrum_point=point WHERE spectrum_point='' OR spectrum_point IS NULL")
    for column in ("iq_relative_directory", "iq_recording_prefix"):
        if column not in location_columns:
            connection.execute(f"ALTER TABLE scene_locations ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS scene_catalog_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)"
    )
    link_columns = {row[1] for row in connection.execute("PRAGMA table_info(iq_location_links)")}
    for column in ("wsm_file", "ws1_file", "ws2_file"):
        if column not in link_columns:
            connection.execute(f"ALTER TABLE iq_location_links ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    return connection


def suggest_scene_type(point: str) -> str:
    rules = (
        (("工业园", "工厂"), "工业区"),
        (("创新中心", "总部基地"), "商务办公区"),
        (("万达广场地下停车场",), "大型停车场"),
        (("停车场",), "大型停车场"),
        (("购物广场", "万达"), "购物广场"),
        (("地铁站",), "地铁站"),
        (("轨道交通集团",), "轻轨运营中心"),
        (("机场", "航站楼"), "机场区域"),
        (("医院",), "医院外部"),
        (("电台",), "短波电台"),
        (("广播电视塔", "电视塔"), "广播电视塔"),
        (("热电厂", "发电厂", "发电站"), "发电站"),
        (("Apollo", "智能网联"), "智能网联示范区"),
        (("物流园",), "物流仓储中心"),
        (("长春西站", "丰台站"), "大型交通枢纽"),
        (("八一水韵城",), "高密度居民区"),
        (("家园", "嘉园", "家属院", "小区", "天华二里"), "居民区"),
        (("图书馆", "博物馆"), "公共文化区域"),
        (("商圈",), "闹市区"),
    )
    for keywords, scene_type in rules:
        if any(keyword.casefold() in point.casefold() for keyword in keywords):
            return scene_type
    return "未分类"


def initialize_scene_catalog(database_path: Path, locations: Iterable[tuple[str, str]]) -> int:
    connection = _connect(database_path)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        managed = connection.execute(
            "SELECT setting_value FROM scene_catalog_settings WHERE setting_key='association_table_managed'"
        ).fetchone()
        if managed and managed[0] == "1":
            return int(connection.execute("SELECT COUNT(*) FROM scene_locations").fetchone()[0])
        for city, point in sorted(set(locations)):
            suggested = suggest_scene_type(point)
            connection.execute(
                """
                INSERT OR IGNORE INTO scene_locations
                    (city, point, scene_type, classification_source, notes, updated_at, spectrum_point)
                VALUES (?, ?, ?, '名称规则建议', '', ?, ?)
                """,
                (city, point, suggested, now, point),
            )
            connection.execute(
                """
                UPDATE scene_locations SET scene_type=?, updated_at=?
                WHERE city=? AND point=? AND classification_source='名称规则建议'
                """,
                (suggested, now, city, point),
            )
        connection.commit()
        return int(connection.execute("SELECT COUNT(*) FROM scene_locations").fetchone()[0])
    finally:
        connection.close()


def list_scene_locations(
    database_path: Path,
    scene_type: str = "全部",
    city: str = "全部",
    keyword: str = "",
) -> list[SceneLocation]:
    connection = _connect(database_path)
    clauses: list[str] = []
    parameters: list[str] = []
    if scene_type and scene_type != "全部":
        clauses.append("scene_type = ?")
        parameters.append(scene_type)
    if city and city != "全部":
        clauses.append("city = ?")
        parameters.append(city)
    if keyword.strip():
        clauses.append("point LIKE ?")
        parameters.append(f"%{keyword.strip()}%")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    try:
        rows = connection.execute(
            "SELECT city, point, scene_type, classification_source, notes, updated_at, spectrum_point, "
            "iq_relative_directory, iq_recording_prefix "
            "FROM scene_locations" + where + " ORDER BY scene_type, city, point",
            parameters,
        ).fetchall()
        return [SceneLocation(*row) for row in rows]
    finally:
        connection.close()


def scene_filter_values(database_path: Path) -> tuple[list[str], list[str]]:
    connection = _connect(database_path)
    try:
        imported_scenes = [row[0] for row in connection.execute("SELECT DISTINCT scene_type FROM scene_locations ORDER BY scene_type")]
        scenes = [*SCENE_TYPES, *(scene for scene in imported_scenes if scene not in SCENE_TYPES)]
        cities = [row[0] for row in connection.execute("SELECT DISTINCT city FROM scene_locations ORDER BY city")]
        return ["全部", *scenes], ["全部", *cities]
    finally:
        connection.close()


def update_scene_assignment(
    database_path: Path,
    city: str,
    point: str,
    scene_type: str,
    notes: str = "",
) -> None:
    if scene_type not in SCENE_TYPES:
        raise ValueError(f"不支持的场景类型：{scene_type}")
    connection = _connect(database_path)
    try:
        connection.execute(
            """
            UPDATE scene_locations
            SET scene_type=?, classification_source='人工指定', notes=?, updated_at=?
            WHERE city=? AND point=?
            """,
            (scene_type, notes.strip(), datetime.now().isoformat(timespec="seconds"), city, point),
        )
        connection.commit()
    finally:
        connection.close()


def link_iq_recording(
    database_path: Path,
    city: str,
    point: str,
    recording_stem: str,
    wsm_file: str = "",
    ws1_file: str = "",
    ws2_file: str = "",
) -> None:
    connection = _connect(database_path)
    try:
        connection.execute(
            """
            INSERT INTO iq_location_links(city, point, recording_stem, wsm_file, ws1_file, ws2_file, linked_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(city, point, recording_stem) DO UPDATE SET
                wsm_file=excluded.wsm_file, ws1_file=excluded.ws1_file, ws2_file=excluded.ws2_file,
                linked_at=excluded.linked_at
            """,
            (
                city, point, recording_stem, wsm_file, ws1_file, ws2_file,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def unlink_iq_recording(database_path: Path, city: str, point: str, recording_stem: str) -> None:
    connection = _connect(database_path)
    try:
        connection.execute(
            "DELETE FROM iq_location_links WHERE city=? AND point=? AND recording_stem=?",
            (city, point, recording_stem),
        )
        connection.commit()
    finally:
        connection.close()


def list_linked_iq(database_path: Path, city: str, point: str) -> list[str]:
    connection = _connect(database_path)
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT recording_stem FROM iq_location_links WHERE city=? AND point=? ORDER BY recording_stem",
                (city, point),
            )
        ]
    finally:
        connection.close()


def list_linked_iq_details(database_path: Path, city: str, point: str) -> list[IQLocationLink]:
    connection = _connect(database_path)
    try:
        rows = connection.execute(
            """
            SELECT recording_stem, wsm_file, ws1_file, ws2_file
            FROM iq_location_links WHERE city=? AND point=? ORDER BY recording_stem
            """,
            (city, point),
        ).fetchall()
        return [IQLocationLink(*row) for row in rows]
    finally:
        connection.close()


def discover_iq_groups_by_prefix(iq_root: Path, relative_directory: str, prefix: str) -> list[IQLocationLink]:
    """Find every complete .wsm/.ws1/.ws2 group whose stem starts with prefix."""
    relative = Path(relative_directory) if relative_directory and relative_directory != "." else Path()
    root = iq_root.resolve()
    candidates = [(root / relative).resolve()]
    if relative != Path():
        if root.name.casefold() == str(relative).casefold():
            candidates.append(root)
        candidates.append((root.parent / relative).resolve())
    folders: list[Path] = []
    for candidate in candidates:
        if candidate.is_dir() and candidate not in folders:
            folders.append(candidate)
    if not folders:
        return []

    prefix_key = prefix.casefold()
    frequency_suffix = re.compile(r"\d+(?:\.\d+)?m?", re.IGNORECASE)
    groups: list[IQLocationLink] = []
    for folder in folders:
        stems = sorted(
            {
                path.stem
                for path in folder.iterdir()
                if path.is_file()
                and path.suffix.casefold() == ".wsm"
                and path.stem.casefold().startswith(prefix_key)
                and frequency_suffix.fullmatch(path.stem[len(prefix):]) is not None
            },
            key=str.casefold,
        )
        for stem in stems:
            files = {suffix: folder / f"{stem}{suffix}" for suffix in (".wsm", ".ws1", ".ws2")}
            if not all(path.is_file() for path in files.values()):
                continue
            groups.append(
                IQLocationLink(
                    recording_stem=stem,
                    wsm_file=str(files[".wsm"]),
                    ws1_file=str(files[".ws1"]),
                    ws2_file=str(files[".ws2"]),
                )
            )
    return groups


def write_association_template(database_path: Path, output_path: Path) -> int:
    """Export all catalog locations and existing IQ links as an editable CSV."""
    locations = list_scene_locations(database_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ASSOCIATION_COLUMNS)
        writer.writeheader()
        for serial, location in enumerate(locations, start=1):
            links = list_linked_iq_details(database_path, location.city, location.point) or [IQLocationLink("", "", "", "")]
            for link in links:
                writer.writerow(
                    {
                        "序号": serial,
                        "城市": location.city,
                        "地点": location.point,
                        "场景类型": location.scene_type,
                        "频谱数据目录名称": location.spectrum_point,
                        "IQ数据相对目录": (
                            str(Path(link.wsm_file).parent) if link.wsm_file else location.iq_relative_directory
                        ),
                        "IQ数据组名称": link.recording_stem or location.iq_recording_prefix,
                        "备注": location.notes,
                    }
                )
                row_count += 1
    return row_count


def sync_association_locations(database_path: Path, input_path: Path) -> int:
    """Upsert association-table metadata without deleting locations or existing expanded IQ links."""
    if not input_path.is_file():
        return 0
    connection = _connect(database_path)
    count = 0
    try:
        with input_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [column for column in ASSOCIATION_COLUMNS if column not in (reader.fieldnames or ())]
            if missing:
                raise ValueError("关联表缺少列：" + "、".join(missing))
            for row in reader:
                city = (row.get("城市") or "").strip()
                point = (row.get("地点") or "").strip()
                if not city or not point:
                    continue
                scene_type = (row.get("场景类型") or "").strip() or suggest_scene_type(point)
                spectrum_point = (row.get("频谱数据目录名称") or "").strip()
                iq_directory = (row.get("IQ数据相对目录") or "").strip()
                recording_prefix = (row.get("IQ数据组名称") or "").strip()
                notes = (row.get("备注") or "").strip()
                now = datetime.now().isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO scene_locations (
                        city, point, scene_type, classification_source, notes, updated_at,
                        spectrum_point, iq_relative_directory, iq_recording_prefix
                    ) VALUES (?, ?, ?, '默认关联表同步', ?, ?, ?, ?, ?)
                    ON CONFLICT(city, point) DO UPDATE SET
                        scene_type=excluded.scene_type,
                        classification_source='默认关联表同步',
                        notes=excluded.notes,
                        updated_at=excluded.updated_at,
                        spectrum_point=excluded.spectrum_point,
                        iq_relative_directory=excluded.iq_relative_directory,
                        iq_recording_prefix=excluded.iq_recording_prefix
                    """,
                    (city, point, scene_type, notes, now, spectrum_point, iq_directory, recording_prefix),
                )
                count += 1
        connection.execute(
            """
            INSERT INTO scene_catalog_settings(setting_key, setting_value)
            VALUES ('association_table_managed', '1')
            ON CONFLICT(setting_key) DO UPDATE SET setting_value='1'
            """
        )
        connection.commit()
        return count
    finally:
        connection.close()


def import_association_csv(
    database_path: Path,
    input_path: Path,
    iq_root: Path | None = None,
) -> AssociationImportResult:
    """Import locations, classifications, and optional IQ links from a UTF-8 CSV."""
    errors: list[str] = []
    location_keys: set[tuple[str, str]] = set()
    link_count = 0
    skipped_link_count = 0
    row_count = 0
    connection = _connect(database_path)
    try:
        with input_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [column for column in ASSOCIATION_COLUMNS if column not in (reader.fieldnames or ())]
            if missing:
                raise ValueError("关联表缺少列：" + "、".join(missing))
            connection.execute("DELETE FROM scene_locations")
            connection.execute(
                """
                INSERT INTO scene_catalog_settings(setting_key, setting_value)
                VALUES ('association_table_managed', '1')
                ON CONFLICT(setting_key) DO UPDATE SET setting_value='1'
                """
            )
            for line_number, row in enumerate(reader, start=2):
                row_count += 1
                city = (row.get("城市") or "").strip()
                point = (row.get("地点") or "").strip()
                scene_type = (row.get("场景类型") or "").strip()
                spectrum_point = (row.get("频谱数据目录名称") or "").strip()
                iq_directory = (row.get("IQ数据相对目录") or "").strip()
                recording_stem = (row.get("IQ数据组名称") or "").strip()
                notes = (row.get("备注") or "").strip()
                if not city or not point:
                    errors.append(f"第 {line_number} 行：城市和地点不能为空")
                    continue
                if not scene_type:
                    scene_type = suggest_scene_type(point)
                now = datetime.now().isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO scene_locations
                        (city, point, scene_type, classification_source, notes, updated_at, spectrum_point,
                         iq_relative_directory, iq_recording_prefix)
                    VALUES (?, ?, ?, '关联表导入', ?, ?, ?, ?, ?)
                    ON CONFLICT(city, point) DO UPDATE SET
                        scene_type=excluded.scene_type,
                        classification_source='关联表导入',
                        notes=excluded.notes,
                        updated_at=excluded.updated_at,
                        spectrum_point=excluded.spectrum_point,
                        iq_relative_directory=excluded.iq_relative_directory,
                        iq_recording_prefix=excluded.iq_recording_prefix
                    """,
                    (city, point, scene_type, notes, now, spectrum_point, iq_directory, recording_stem),
                )
                location_keys.add((city, point))
                if not recording_stem:
                    continue
                if iq_root is None:
                    errors.append(f"第 {line_number} 行：未指定IQ根目录，无法展开IQ数据前缀“{recording_stem}”")
                    skipped_link_count += 1
                    continue
                groups = discover_iq_groups_by_prefix(iq_root, iq_directory, recording_stem)
                if not groups:
                    errors.append(
                        f"第 {line_number} 行：没有找到以“{recording_stem}”开头且同时包含 .wsm/.ws1/.ws2 的完整IQ数据组"
                    )
                    skipped_link_count += 1
                    continue
                for group in groups:
                    cursor = connection.execute(
                        """
                        INSERT INTO iq_location_links
                            (city, point, recording_stem, wsm_file, ws1_file, ws2_file, linked_at)
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(city, point, recording_stem) DO UPDATE SET
                            wsm_file=excluded.wsm_file, ws1_file=excluded.ws1_file,
                            ws2_file=excluded.ws2_file, linked_at=excluded.linked_at
                        """,
                        (
                            city, point, group.recording_stem, group.wsm_file,
                            group.ws1_file, group.ws2_file, now,
                        ),
                    )
                    if cursor.rowcount:
                        link_count += 1
            connection.commit()
    finally:
        connection.close()
    return AssociationImportResult(
        row_count=row_count,
        location_count=len(location_keys),
        link_count=link_count,
        skipped_link_count=skipped_link_count,
        errors=tuple(errors),
    )
