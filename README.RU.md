# JetBrains Proxy Auth Patcher

[English](README.md) | **Русский**

Утилита с интерактивным TUI и CLI для изменения поведения `IdeProxyAuthenticator` в совместимых установках IDE на платформе JetBrains. Патч предотвращает автоматическое появление диалога прокси-авторизации, когда сохранённые credentials отсутствуют или не содержат пароль.

> Проект не связан с JetBrains. Патч изменяет файлы установки IDE и может быть перезаписан при обновлении. Перед использованием закройте IDE и сохраните резервную копию важных данных.

## Возможности

- автоматическое обнаружение IDE в стандартных каталогах Windows, macOS и Linux, включая JetBrains Toolbox;
- полноэкранный TUI с множественным выбором, подтверждением операций и журналом;
- CLI для установки, проверки состояния и восстановления;
- резервное копирование и атомарная замена JAR;
- проверка структуры байткода, целостности ZIP и загрузки изменённого класса через `-Xverify:all`;
- автоматическое обновление предыдущих версий патча;
- отдельный низкоуровневый патчер для одного JAR.

## Требования

- Python 3.9 или новее;
- зависимости из `requirements.txt` для полноэкранного TUI;
- CLI-команды `list`, `status`, `patch` и `restore` не требуют Textual.

## Запуск TUI

```sh
python -m pip install -r requirements.txt
python jetbrains_proxy.py
```

Управление:

| Клавиша | Действие |
|---|---|
| `↑` / `↓` | Навигация по списку |
| `Space` | Выбрать или снять выбор |
| `A` | Выбрать все / снять общий выбор |
| `P` | Установить патч |
| `R` | Восстановить оригинальный JAR |
| `U` | Повторить поиск IDE |
| `Q` | Выйти |

Patch и restore выполняются после подтверждения в модальном окне. Операции запускаются в фоновом worker, поэтому интерфейс остаётся доступным.

## CLI

Показать обнаруженные установки и их состояние:

```sh
python jetbrains_proxy.py list
python jetbrains_proxy.py status
```

Установить патч по ID из списка:

```sh
python jetbrains_proxy.py patch <ide-id>
```

Можно указать несколько ID. Также поддерживаются product code, имя IDE и путь установки:

```sh
python jetbrains_proxy.py patch <ide-id-1> <ide-id-2>
python jetbrains_proxy.py patch <product-code>
```

Обработать все найденные совместимые установки:

```sh
python jetbrains_proxy.py patch --all
```

Восстановить оригинальный JAR:

```sh
python jetbrains_proxy.py restore <ide-id>
python jetbrains_proxy.py restore --all
```

Добавить каталог для поиска:

```sh
python jetbrains_proxy.py --root "<INSTALLATION_PARENT>" list
python jetbrains_proxy.py --root "<IDE_HOME>" patch <ide-id>
```

Параметр `--root` можно указывать несколько раз. Перед patch/restore рекомендуется закрыть выбранную IDE. Флаг `--force` пропускает только проверку запущенного процесса и не снимает файловые блокировки операционной системы.

### Состояния

- `original` — исходный байткод;
- `patched` — патч установлен;
- `broken` — обнаружен некорректный ранний вариант патча;
- `unsupported` — структура JAR или класса не поддерживается.

Предыдущие версии патча и служебные ZIP-метаданные обновляются автоматически без отдельного состояния.

## Сборка исполняемого файла

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

### Linux и macOS

```sh
chmod +x build.sh
./build.sh
```

По умолчанию скрипт использует `python3`. Другой интерпретатор можно выбрать через `PYTHON_BIN`:

```sh
PYTHON_BIN=python3.12 ./build.sh
```

Скрипты создают `.venv`, устанавливают зависимости и PyInstaller, после чего собирают standalone-приложение в `dist`:

```text
dist/JetBrainsProxyPatcher.exe  (Windows)
dist/JetBrainsProxyPatcher      (Linux/macOS)
```

Запуск в Linux и macOS:

```sh
./dist/JetBrainsProxyPatcher
./dist/JetBrainsProxyPatcher list
./dist/JetBrainsProxyPatcher patch <ide-id>
```

В Windows EXE можно запустить без аргументов для открытия TUI или использовать с теми же CLI-командами:

```powershell
JetBrainsProxyPatcher.exe list
JetBrainsProxyPatcher.exe patch <ide-id>
```

## Низкоуровневое использование

Файл `patch_ide_proxy.py` позволяет работать непосредственно с `intellij.platform.ide.jar`.

Создать отдельную изменённую копию:

```sh
python patch_ide_proxy.py "<IDE_HOME>/lib/intellij.platform.ide.jar" --output patched.jar
```

Изменить JAR на месте:

```sh
python patch_ide_proxy.py "<IDE_HOME>/lib/intellij.platform.ide.jar"
```

При установке на месте создаётся резервная копия `intellij.platform.ide.jar.bak`. Повторная обработка актуального JAR возвращает `already patched`.

Для отката через основной интерфейс предпочтительнее использовать команду `restore`, а не ручное копирование файлов.

## Изменяемое поведение

Исходная логика метода `requestPasswordAuthenticationInstance` условно выглядит так:

```java
Credentials credentials = proxyAuth.getKnownAuthentication(hostName, port);
if (credentials == null || credentials.getUserName() == null || credentials.getPassword() == null) {
    credentials = proxyAuth.getPromptedAuthentication(prompt, hostName, port);
}
```

При этом отсутствующий пароль уже корректно обрабатывается ниже:

```java
OneTimeString password = credentials.getPassword();
return new PasswordAuthentication(
    credentials.getUserName(),
    password != null ? password.toCharArray() : new char[0]
);
```

Патч изменяет два перехода:

1. Если `getKnownAuthentication()` возвращает `null`, метод возвращает `null` без вызова prompted authentication.
2. Проверка `getPassword() == null` пропускается, после чего штатный код создаёт пустой массив пароля.

Проверка `getUserName() == null` сохраняется.

### Ограничение

Автоматический диалог JDK `Authenticator` больше не используется для первоначального ввода отсутствующих credentials. Если прокси требует авторизацию, логин и пароль следует указать в настройках прокси IDE.

## Безопасность и реализация

Патчер разбирает constant pool, инструкции целевого метода и `StackMapTable`. Изменения выполняются только при обнаружении ожидаемой JVM-сигнатуры и структуры байткода. На неизвестной версии класса операция завершается с ошибкой.

При изменении кода сохраняются смещения последующих инструкций. `StackMapTable` корректируется для соответствия новому потоку управления. При записи JAR удаляется служебный ZIP-комментарий JetBrains IKV, поскольку его абсолютные смещения central directory становятся недействительными после изменения размера entry.

Перед установкой выполняются:

- проверка целостности ZIP;
- повторный разбор изменённого класса;
- загрузка класса bundled JetBrains Runtime с `-Xverify:all`, если runtime найден;
- атомарная замена JAR с сохранением backup.

Резервная копия расположена рядом с JAR и имеет суффикс `.bak`. Обновление IDE может восстановить исходный JAR и удалить установленный патч.
