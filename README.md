# Blender MCP

Подключение Blender к ассистенту через **MCP (Model Context Protocol)**: ассистент видит сцену,
создаёт и правит объекты, назначает материалы, рендерит кадры, снимает вьюпорт и может выполнять
произвольный `bpy`-код.

Состоит из двух частей:

| Часть | Файл | Что делает |
| --- | --- | --- |
| Аддон Blender | `addon/blender_mcp_addon.py` | Поднимает внутри Blender TCP-сервер на `127.0.0.1:9876` и выполняет команды в главном потоке |
| MCP-сервер | `src/blender_mcp/` | Запускается MCP-клиентом (Claude Code / Claude Desktop / Cursor), переводит вызовы инструментов в команды аддона |

```
MCP-клиент  ──stdio──>  blender_mcp  ──TCP JSON──>  аддон в Blender  ──>  bpy
```

## Установка

### 1. Аддон в Blender

1. Blender → `Edit ▸ Preferences ▸ Add-ons ▸ Install…` (в Blender 4.2+ — кнопка ⌄ → `Install from Disk…`).
2. Выберите файл `addon/blender_mcp_addon.py`.
3. Включите галочку у аддона **Interface: Blender MCP**.
4. В 3D-вьюпорте нажмите `N`, откройте вкладку **MCP** и нажмите **Start server**.

В панели видно порт (по умолчанию 9876) и переключатель **Allow Python execution**.

### 2. MCP-сервер

Нужен Python 3.10+. Через [`uv`](https://docs.astral.sh/uv/) устанавливать ничего не требуется —
клиент запустит сервер сам. Для локальной установки:

```bash
uv venv && uv pip install -e .
# или: python -m venv .venv && .venv/bin/pip install -e .
```

## Подключение к клиенту

### Claude Code

Из корня репозитория:

```bash
claude mcp add blender -- uv --directory /абсолютный/путь/к/Blender run blender-mcp
```

В репозитории уже лежит `.mcp.json` с этой же конфигурацией (project scope) — если открыть проект
в Claude Code, сервер подхватится автоматически, нужно только подтвердить его использование.
Проверить: `/mcp` в Claude Code — сервер `blender` должен быть `connected`.

### Claude Desktop

`Settings ▸ Developer ▸ Edit Config`, в `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "blender": {
      "command": "uv",
      "args": ["--directory", "/абсолютный/путь/к/Blender", "run", "blender-mcp"]
    }
  }
}
```

### Cursor / другие клиенты

Тот же блок `mcpServers` в конфиге клиента. Транспорт — stdio, команда запуска —
`uv --directory <путь> run blender-mcp` или `<путь>/.venv/bin/python -m blender_mcp`.

### Переменные окружения

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `BLENDER_MCP_HOST` | `127.0.0.1` | Хост аддона |
| `BLENDER_MCP_PORT` | `9876` | Порт (должен совпадать с портом в панели аддона) |
| `BLENDER_MCP_TIMEOUT` | `60` | Таймаут команды по умолчанию, сек |

## Инструменты

| Инструмент | Назначение |
| --- | --- |
| `blender_ping` | Проверка связи, версия Blender, открытый файл |
| `blender_get_scene_info` | Сцена целиком: движок, кадры, камера, список объектов (с пагинацией и фильтрами) |
| `blender_get_object_info` | Один объект: трансформ, статистика меша, модификаторы, материалы, bounding box |
| `blender_create_object` | Примитивы (CUBE, SPHERE, CYLINDER, …), EMPTY, CAMERA, LIGHT, TEXT |
| `blender_modify_object` | Позиция, поворот, масштаб, имя, родитель, видимость |
| `blender_delete_object` | Удаление объекта |
| `blender_set_material` | Создание/переиспользование материала Principled BSDF: цвет, metallic, roughness, alpha, emission |
| `blender_execute_python` | Произвольный `bpy`-код (модификаторы, ноды, анимация, импорт/экспорт) |
| `blender_render_image` | Рендер с активной камеры + картинка в ответе |
| `blender_get_viewport_screenshot` | Скриншот 3D-вьюпорта + картинка в ответе |

Примеры запросов: «что сейчас в сцене?», «добавь красную сферу над кубом», «поставь камеру и отрендери
превью в EEVEE», «навесь на Suzanne subdivision surface и покажи вьюпорт».

## Безопасность

- Сервер слушает только `127.0.0.1` и не требует авторизации: любой процесс на этой машине может к
  нему подключиться, пока он запущен. Останавливайте его кнопкой **Stop server**, когда он не нужен.
- `blender_execute_python` выполняет **любой** код в вашей сессии Blender. Это самый мощный и самый
  опасный инструмент: он может изменить или испортить открытый файл. Выключается галочкой
  **Allow Python execution** в панели аддона — тогда инструмент возвращает понятную ошибку, а
  остальные продолжают работать.
- Сохраняйтесь перед сессией: отменить изменения можно только через `Ctrl+Z` в самом Blender.

## Разработка и тесты

```bash
uv pip install -e ".[dev]"
uv run pytest -q
```

Тесты гоняют настоящий протокол через настоящий TCP-сокет. Если в окружении установлен пакет
[`bpy`](https://pypi.org/project/bpy/) (Blender как модуль Python), обработчики аддона проверяются
против реального `bpy` API; если нет — эти тесты пропускаются, а протокольные продолжают работать:

```bash
uv pip install bpy   # требует ровно ту версию Python, под которую собран wheel
```

Протокольные и клиентские тесты идут через настоящий сокет, а обработчики аддона прогнаны
против `bpy` 5.0 (включая реальный рендер в Cycles). Минимальная заявленная версия Blender — 3.2
(нужен `bpy.context.temp_override`).

## Диагностика

| Симптом | Что делать |
| --- | --- |
| `No Blender MCP server is listening on 127.0.0.1:9876` | Blender запущен? Аддон включён? Нажата **Start server**? |
| Порт занят | Смените порт в панели аддона и задайте `BLENDER_MCP_PORT` в конфиге клиента |
| `Python execution is disabled` | Включите **Allow Python execution** в панели |
| Долгий рендер обрывается по таймауту | Передайте инструменту `timeout` побольше или уменьшите `samples` |
| `no 3D viewport is open` | Blender запущен в фоновом режиме — используйте `blender_render_image` |
| Ошибки аддона | Смотрите системную консоль Blender: `Window ▸ Toggle System Console` (Windows) или запуск из терминала (Linux/macOS) |

## Лицензия

MIT.
