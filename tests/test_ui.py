"""验证MSS 的编辑块、宿主沙箱和 wheel 分发。"""

from html.parser import HTMLParser
from pathlib import Path
from shutil import copy2, copytree
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import pytest
from app.api import plan_router, plugin_router, scripts_router
from app.api._config_editors import config_detail_ui
from app.config import ConfigCollection, config_manager
from app.core import Config, Plugin
from app.core.i18n import I18nManager
from app.models.config import PluginRecord, PluginRegistryCollection
from app.models.schema import DeclarativeBlockOut, HostBlockOut, PluginBlockOut
from app.plugin.calls import CallGate
from app.plugin.frontend import MountedFrontend
from app.plugin.http import HttpRegistry
from app.plugin.types import PluginState
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import mss_adapter
from mss_adapter.config import MSSConfig, MSSPlanConfig
from mss_adapter.plugin import Plugin as Adapter

NAME = "mss_adapter"
SOURCE = Path(__file__).resolve().parents[1]
PACKAGE = Path(mss_adapter.__file__).resolve().parent
ASSETS = {
    "script-src": ["_host/panel-sdk.js", "assets/main.js"],
    "style-src": ["_host/panel-base.css", "assets/main.css"],
}
PAGES = {"script", "user", "plan"}
LOCALES = ("zh-CN", "en-US", "ja-JP")


class PanelAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.files = {"script-src": [], "style-src": []}

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        assert tag != "style"
        assert not any(name == "style" or name.startswith("on") for name in fields)
        if tag == "script":
            assert fields.get("type") != "module" and "crossorigin" not in fields
            assert fields["src"].startswith("./")
            if fields["src"] != "./_host/panel-sdk.js":
                assert "defer" in fields
            self.files["script-src"].append(fields["src"][2:])
        if tag == "link" and fields.get("rel") == "stylesheet":
            assert fields["href"].startswith("./")
            self.files["style-src"].append(fields["href"][2:])


async def mount_frontend(monkeypatch):
    scripts = ConfigCollection.build([MSSConfig])
    plans = ConfigCollection.build([MSSPlanConfig])
    registry = PluginRegistryCollection.build()
    await plans.activate()
    await scripts.activate()
    await registry.activate()
    record_id = registry.add(
        PluginRecord,
        payload={"info": {"plugin_name": NAME, "state": PluginState.ENABLED}},
    )
    await registry.commit()
    adapter = Adapter()
    record = registry[record_id]
    record._plugin_cls = Adapter
    record._plugin = adapter
    monkeypatch.setattr(Config, "PlanConfig", plans)
    monkeypatch.setitem(config_manager._collections, "PlanConfig", plans)
    monkeypatch.setattr(Config, "ScriptConfig", scripts)
    monkeypatch.setitem(config_manager._collections, "ScriptConfig", scripts)
    monkeypatch.setattr(Config, "plugin_registry", registry)
    monkeypatch.setattr(Plugin, "script_types", {record.uid: Adapter.decl})
    await adapter.pre_enable()

    gate = CallGate()
    monkeypatch.setitem(Plugin._calls, record.uid, gate)
    frontend = MountedFrontend.load(
        PACKAGE, NAME, "MSS", "MSSPlanConfig", gate, block_pages=PAGES
    )
    assert frontend is not None
    assert frontend.diagnostics == []
    assert frontend.block_pages == PAGES

    app = FastAPI()
    app.include_router(plugin_router)
    app.include_router(scripts_router)
    app.include_router(plan_router)
    http = HttpRegistry()
    monkeypatch.setattr(Plugin, "http", http)
    http.bind_app(app)
    http.mount(NAME, [], router=Adapter.api_router, frontend=frontend, gate=gate)
    return app, frontend


@pytest.mark.asyncio
async def test_built_panels_served_under_host_csp(monkeypatch, tmp_path):
    app, frontend = await mount_frontend(monkeypatch)
    root = f"/api/plugin/{NAME}/ui/{frontend.revision}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for page in sorted(PAGES):
            response = await client.get(f"{root}/{page}.html")
            assert response.status_code == 200
            policy = dict(
                directive.split(" ", 1)
                for directive in response.headers["Content-Security-Policy"].split("; ")
            )
            assert policy["default-src"] == "'none'"
            assert policy["sandbox"] == "allow-scripts allow-forms"
            assert all(
                source.startswith(f"http://test{root}/")
                for source in policy["connect-src"].split()
            )
            assets = PanelAssets()
            assets.feed(response.text)
            assert assets.files == ASSETS
            for directive, mime in (
                ("script-src", "text/javascript"),
                ("style-src", "text/css"),
            ):
                assert "'unsafe-inline'" not in policy[directive].split()
                for path in assets.files[directive]:
                    target = f"http://test{root}/{path}"
                    assert target in policy[directive].split()
                    asset = await client.get(target)
                    assert asset.status_code == 200
                    assert asset.headers["Content-Type"].startswith(mime)

    for filename in ("zh_CN.json", "en.json", "ja.json"):
        (tmp_path / filename).write_text('{"core.test": "host"}', encoding="utf-8")
    manager = I18nManager(tmp_path)
    manager.set_package_roots({NAME: PACKAGE})
    for locale in LOCALES:
        messages, _ = await manager.get_messages(locale)
        panel = messages["plugins"][NAME]["panel"]
        assert panel["project"] and panel["queue"] and panel["plan"]["TribulationStage"]
        assert panel["mss"]["planHint"]


@pytest.mark.asyncio
async def test_editor_blocks_and_scoped_routes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    app, frontend = await mount_frontend(monkeypatch)
    assert Adapter.decl.first_account_mode == "wizard"
    script_uid, script = await Config.add_script("MSS")
    script_id = str(script_uid)
    user_uid, user = await Config.add_user(script_id)
    user_id = str(user_uid)
    plan_uid, plan = await Config.add_plan("MSSPlanConfig")
    plan_id = str(plan_uid)
    _, plan_docs = await Config.get_plan(plan_id)
    plan_ui = config_detail_ui(plan, plan_docs)
    plan_mount = frontend.surface_mount(name=NAME, page="plan")
    assert plan_ui.frontendSurfaces["plan"] == plan_mount
    assert plan_ui.blocks == [
        DeclarativeBlockOut(groups=["Info"]),
        PluginBlockOut(
            pageId="plan",
            fallbackGroups=[
                "ALL",
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
                *(f"Day{i}" for i in range(1, 32)),
            ],
            mount=plan_mount,
        ),
    ]
    assert any(
        hint["field"] == "Key" and hint["editable"] for hint in plan_ui.hints["ALL"]
    )

    _, script_docs = await Config.get_script(script_id)
    assert config_detail_ui(script, script_docs).blocks == [
        PluginBlockOut(
            pageId="script",
            fallbackGroups=["Info", "Embedded", "Device", "Game", "Run"],
            mount=frontend.surface_mount(name=NAME, page="script"),
        ),
        HostBlockOut(name="game_binding"),
        DeclarativeBlockOut(groups=["Update"]),
    ]
    _, user_docs = await Config.get_user(script_id, user_id)
    ui = config_detail_ui(user, user_docs)
    assert ui.blocks == [
        HostBlockOut(name="config_restore"),
        DeclarativeBlockOut(groups=["Info"]),
        PluginBlockOut(
            pageId="user",
            fallbackGroups=["Task", "Device"],
            mount=frontend.surface_mount(name=NAME, page="user"),
        ),
        DeclarativeBlockOut(groups=["Notify"]),
        HostBlockOut(name="webhooks"),
    ]
    assert [(pool.key, pool.kind) for pool in ui.backupTargets] == [
        ("mas", "user"),
        ("native", "script"),
    ]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        editor = f"/api/plugin/{NAME}/editor"
        headers = {
            "X-Plugin-Ui-Provider": NAME,
            "X-Plugin-Ui-Activation": frontend.activation_id,
            "X-Plugin-Ui-Page": "script",
        }
        script_url = f"{editor}/script/{script_id}"
        user_url = f"{editor}/user/{script_id}/{user_id}"
        assert (await client.get(script_url, headers=headers)).json() == {
            "path": None,
            "preferAdb": False,
            "preview": None,
        }
        plan_url = f"{editor}/plan/{plan_id}"
        assert (await client.get(user_url, headers=headers)).status_code == 403
        assert (await client.get(plan_url, headers=headers)).status_code == 403
        headers["X-Plugin-Ui-Page"] = "user"
        response = await client.get(user_url, headers=headers)
        assert response.status_code == 200, response.text
        options = [{"label": plan.Info.Name, "value": plan_id}]
        assert response.json()["plans"] == options
        assert (
            await client.get(f"{user_url}/plans", headers=headers)
        ).json() == options
        response = await client.put(
            f"{user_url}/flavor",
            headers=headers,
            json={"planMode": plan_id, "activityFirst": False},
        )
        assert response.status_code == 200, response.text
        assert user.Info.PlanMode == plan_uid
        assert user.Info.IfActivityFirst is False
        templates = script.Task.Templates
        response = await client.put(
            f"{user_url}/templates",
            headers=headers,
            json={"before": templates, "templates": []},
        )
        assert response.status_code == 200, response.text
        assert response.json()["templates"] == script.Task.Templates
        response = await client.post(
            "/api/scripts/user/update",
            headers=headers,
            json={
                "scriptId": script_id,
                "userId": user_id,
                "data": {"Device": {"AdbAddress": "127.0.0.1:5555"}},
            },
        )
        assert response.status_code == response.json()["code"] == 200, response.text
        assert user.Device.AdbAddress == "127.0.0.1:5555"
        backup = await Config.ensure_config_backup(script_id, user_id, "mas")
        assert backup["created"]
        preview = await Config.get_config_backup_preview(
            script_id, user_id, backup["time"], "mas"
        )
        assert preview["data"]["sections"]
        await Config.update_user(
            script_id, user_id, {"Device": {"AdbAddress": "127.0.0.1:5556"}}
        )
        await Config.restore_config_backup(script_id, user_id, backup["time"], "mas")
        assert user.Device.AdbAddress == "127.0.0.1:5555"
        headers["X-Plugin-Ui-Page"] = "plan"
        response = await client.get(plan_url, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == "ALL"
        result = await client.post(
            "/api/plan/update",
            headers=headers,
            json={"planId": plan_id, "data": {"ALL": {"Key": {"FightTimes": 3}}}},
        )
        assert result.status_code == result.json()["code"] == 200, result.text
        assert plan.ALL.Key.FightTimes == 3
        headers["X-Plugin-Ui-Page"] = "user"
        headers["X-Plugin-Ui-Activation"] = "stale"
        assert (await client.get(user_url, headers=headers)).status_code == 409
        assert all(not path.startswith(editor) for path in app.openapi()["paths"])


def test_wheel_contains_ui_and_locales(monkeypatch):
    from setuptools.build_meta import build_wheel

    with TemporaryDirectory() as directory:
        root = Path(directory)
        sources = root / "sources"
        sources.mkdir()
        for filename in (
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "MANIFEST.in",
            "CHANGELOG.md",
        ):
            copy2(SOURCE / filename, sources)
        copytree(SOURCE / "src" / NAME, sources / "src" / NAME)
        with monkeypatch.context() as patch:
            patch.chdir(sources)
            wheel = root / build_wheel(str(root))
        with ZipFile(wheel) as archive:
            required = {
                *(
                    f"{NAME}/ui/{entry}"
                    for entry in ("manifest.json", *(f"{page}.html" for page in PAGES))
                ),
                f"{NAME}/ui/assets/main.js",
                f"{NAME}/ui/assets/main.css",
                *(f"{NAME}/i18n/{locale}.json" for locale in LOCALES),
            }
            assets = {
                path.relative_to(sources / "src").as_posix()
                for folder in ("ui", "i18n")
                for path in (sources / "src" / NAME / folder).rglob("*")
                if path.is_file()
            }
            assert required <= assets <= set(archive.namelist())
            for path in assets:
                assert archive.read(path) == (sources / "src" / path).read_bytes()
