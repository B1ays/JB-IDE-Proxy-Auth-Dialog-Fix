#!/usr/bin/env python3
"""Full-screen Textual UI for jetbrains_proxy.py."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, RichLog, Static

from jetbrains_proxy import (
    IDEInstallation,
    _sha256,
    discover_ides,
    inspect,
    install_patch,
    is_running,
    restore,
)


_STATE_STYLES = {
    "patched": "bold green",
    "original": "yellow",
    "broken": "bold red",
    "unsupported": "red",
}


class ConfirmScreen(ModalScreen[bool]):
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-dialog {
        width: 72; max-width: 90%; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #confirm-title { text-style: bold; margin-bottom: 1; }
    #confirm-text { margin-bottom: 1; }
    #confirm-buttons { height: auto; align: center middle; }
    #confirm-buttons Button { margin: 0 2; }
    """

    def __init__(self, title: str, text: str, confirm_label: str):
        super().__init__()
        self.title_text = title
        self.body_text = text
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.title_text, id="confirm-title")
            yield Label(self.body_text, id="confirm-text")
            with Horizontal(id="confirm-buttons"):
                yield Button(self.confirm_label, variant="error", id="confirm")
                yield Button("Отмена", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class JetBrainsProxyApp(App):
    TITLE = "JetBrains Proxy Auth Patcher"
    SUB_TITLE = "TUI"
    CSS = """
    Screen { layout: vertical; }
    #summary { height: auto; padding: 0 1; color: $text-muted; }
    #installations { height: 1fr; min-height: 8; margin: 0 1; }
    #details {
        height: auto; max-height: 7; padding: 0 1; margin: 0 1;
        border: round $primary; background: $surface;
    }
    #log {
        height: 12; min-height: 6; margin: 0 1 1 1;
        border: round $secondary; background: $surface;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Выход"),
        Binding("space", "toggle", "Выбрать"),
        Binding("a", "select_all", "Все"),
        Binding("p", "patch", "Патч"),
        Binding("r", "restore", "Восстановить"),
        Binding("u", "rescan", "Обновить"),
    ]

    def __init__(self, installations: list[IDEInstallation], roots: list[Path], force: bool):
        super().__init__()
        self.installations = installations
        self.roots = roots
        self.force = force
        self.selected: set[str] = set()
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="summary")
        yield DataTable(id="installations", cursor_type="row", zebra_stripes=True)
        yield Static("Выберите строку для просмотра сведений.", id="details")
        yield RichLog(id="log", wrap=True, markup=True, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#installations", DataTable)
        table.add_columns("#", "", "ID", "IDE", "Версия", "Состояние", "Backup", "Путь")
        self.refresh_table()
        self.write_log("Готово. Стрелки — навигация, Space — выбор, P — патч, R — восстановление.")
        if not self.installations:
            self.write_log("[yellow]IDE не найдены. Перезапустите с --root ПУТЬ.[/yellow]")
        table.focus()

    def refresh_table(self) -> None:
        table = self.query_one("#installations", DataTable)
        previous = table.cursor_row if table.row_count else 0
        table.clear()
        for number, item in enumerate(self.installations, 1):
            state = inspect(item)
            mark = "✓" if item.id in self.selected else ""
            table.add_row(
                str(number), mark, item.id, item.name, item.version,
                Text(state.state, style=_STATE_STYLES.get(state.state, "")),
                "да" if state.backup else "нет", str(item.root), key=item.id,
            )
        if table.row_count:
            table.move_cursor(row=min(previous, table.row_count - 1))
        self.update_summary()
        self.update_details()

    def update_summary(self) -> None:
        patched = sum(inspect(item).state == "patched" for item in self.installations)
        text = (
            f"Найдено: {len(self.installations)}   Выбрано: {len(self.selected)}   "
            f"Пропатчено: {patched}   Force: {'on' if self.force else 'off'}"
        )
        self.query_one("#summary", Static).update(text)

    def current_installation(self) -> IDEInstallation | None:
        table = self.query_one("#installations", DataTable)
        if not table.row_count or table.cursor_row < 0:
            return None
        return self.installations[table.cursor_row]

    def update_details(self) -> None:
        item = self.current_installation()
        widget = self.query_one("#details", Static)
        if item is None:
            widget.update("IDE не найдены. Используйте CLI --root для добавления каталога поиска.")
            return
        state = inspect(item)
        running = "да" if is_running(item) else "нет"
        widget.update(
            f"[bold]{item.name}[/bold]  [{item.id}]\n"
            f"JAR: {item.jar}\n"
            f"Backup: {item.backup} ({'найден' if state.backup else 'нет'})\n"
            f"Состояние: {state.detail}   Запущена: {running}"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.update_details()

    def action_toggle(self) -> None:
        item = self.current_installation()
        if item:
            self.selected.symmetric_difference_update({item.id})
            self.refresh_table()

    def action_select_all(self) -> None:
        if len(self.selected) == len(self.installations):
            self.selected.clear()
        else:
            self.selected.update(item.id for item in self.installations)
        self.refresh_table()

    def action_rescan(self) -> None:
        if self.busy:
            self.notify("Дождитесь завершения операции", severity="warning")
            return
        self.installations = discover_ides(self.roots)
        self.selected.clear()
        self.refresh_table()
        self.write_log(f"Повторный поиск: найдено {len(self.installations)} установок.")

    def chosen(self) -> list[IDEInstallation]:
        return [item for item in self.installations if item.id in self.selected]

    def action_patch(self) -> None:
        self.confirm_operation("patch")

    def action_restore(self) -> None:
        self.confirm_operation("restore")

    def confirm_operation(self, operation: str) -> None:
        if self.busy:
            self.notify("Другая операция ещё выполняется", severity="warning")
            return
        chosen = self.chosen()
        if not chosen:
            self.notify("Сначала выберите хотя бы одну IDE (Space)", severity="warning")
            return
        is_patch = operation == "patch"
        title = "Установка патча" if is_patch else "Восстановление оригинала"
        names = "\n".join(f"• {item.name} [{item.id}]" for item in chosen)
        warning = "\nЗакройте выбранные IDE перед продолжением."
        self.push_screen(
            ConfirmScreen(title, names + warning, "Патч" if is_patch else "Восстановить"),
            lambda confirmed: self.start_operation(operation, chosen) if confirmed else None,
        )

    def start_operation(self, operation: str, chosen: list[IDEInstallation]) -> None:
        self.busy = True
        self.selected.clear()
        self.refresh_table()
        self.write_log(f"[bold]Начало операции: {operation}; установок: {len(chosen)}[/bold]")
        self.operation_worker(operation, chosen)

    @work(thread=True, exclusive=True, group="ide-operation")
    def operation_worker(self, operation: str, chosen: list[IDEInstallation]) -> None:
        action = install_patch if operation == "patch" else restore
        failures = 0
        for item in chosen:
            self.call_from_thread(self.write_log, f"[cyan]{item.name}[/cyan] [{item.id}]")
            try:
                status = action(item, force=self.force)
                digest = _sha256(item.jar)
                self.call_from_thread(
                    self.write_log, f"[green]OK:[/green] {status}\nSHA-256: {digest}"
                )
            except Exception as error:
                failures += 1
                self.call_from_thread(self.write_log, f"[red]ОШИБКА:[/red] {error}")
        self.call_from_thread(self.operation_finished, failures)

    def operation_finished(self, failures: int) -> None:
        self.busy = False
        self.refresh_table()
        self.query_one("#installations", DataTable).focus()
        if failures:
            self.notify(f"Завершено с ошибками: {failures}", severity="error")
            self.write_log(f"[bold red]Операция завершена; ошибок: {failures}[/bold red]")
        else:
            self.notify("Операция успешно завершена")
            self.write_log("[bold green]Операция успешно завершена.[/bold green]")

    def write_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[dim]{timestamp}[/dim] {message}")


def run_textual(installations: list[IDEInstallation], roots: list[Path], force: bool) -> int:
    JetBrainsProxyApp(installations, roots, force).run()
    return 0
