[English](#english) | [Русский](#russian)

<a id="english"></a>

# Moscow Rent Search

## Project description

Moscow Rent Search is a local web application for filtering long-term rental
listings in Moscow. Draw a search area on the map, set property and price
filters, review matching listings, and export the current results.

## Technology stack

- Python
- FastAPI
- Pydantic
- Jinja2
- Leaflet
- OpenStreetMap
- OpenPyXL
- Pytest

## Features

- Draw an arbitrary search polygon on a Moscow map.
- Filter by property type, area, and maximum rent price.
- Check listing coordinates against the drawn polygon locally.
- Keep listings without coordinates and mark their location as unverified.
- Sort verified in-area listings and unverified listings by price.
- Export the current filtered results to Excel and a standalone HTML file.

## Current source status

- **Test data:** fully working. It provides built-in listings for checking the
  interface and filtering.
- **CIAN:** experimental. Live public access may return Smart CAPTCHA, so real
  listings are not guaranteed.
- **Yandex Realty:** experimental. Live requests may return SSR or meta-refresh
  pages without listing cards, so real listings are not guaranteed.

## Project structure

```text
app/
  main.py                 FastAPI application and HTTP endpoints
  models/                 Listing model
  services/               Search, filtering, and export services
  sources/                Test, CIAN, and Yandex Realty adapters
  static/                 CSS and JavaScript assets
  templates/              Jinja2 templates
tests/
  fixtures/               Saved HTML fixtures for parser tests
  test_*.py               Automated tests
requirements.txt          Application and test dependencies
run.ps1                   Windows launch script
```

## Requirements

- Windows 10/11 with PowerShell.
- Python 3.10 or newer, available as `python` or through the Python Launcher
  (`py`).
- Internet access for the first dependency installation, the map, and live
  source requests.

## Installation and launch

Open PowerShell in the project directory and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

`run.ps1` creates `.venv` when it is missing, installs required dependencies,
selects the next free port when needed, and prints the local URL. To start from
a different preferred port:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Port 8004
```

Stop the server with `Ctrl+C` in the same PowerShell window.

## Usage

1. Select a source and configure the property type, area, and price filters.
2. Draw the search area on the map.
3. Click the search button.
4. Review the result table and optionally hide listings with unverified
   locations.

## Export

After a search returns listings, download the current filtered table as Excel
or standalone HTML. Files are created in `output/` and include the creation
date and time in their names. The Excel file includes formatted headers,
filters, a frozen first row, and clickable links; the HTML file opens without
the local server.

## Supported sources

- Test data
- CIAN (experimental)
- Yandex Realty (experimental)

## Known limitations

- The application does not bypass CAPTCHA, use proxies, authorization, cookies,
  or private APIs.
- External websites can block requests, change their HTML, or load listings
  only with JavaScript after the page opens.
- Listings without coordinates remain in the results, but their location is not
  verified and polygon filtering cannot confirm them.
- Last-search results are stored only in the current process memory and are
  lost after the application restarts.
- Leaflet and OpenStreetMap resources require an internet connection.

## Tests

Run the automated tests after dependencies are installed:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

<a id="russian"></a>

# Moscow Rent Search

## Описание проекта

Moscow Rent Search — локальное веб-приложение для отбора объявлений о
долгосрочной аренде жилья в Москве. Пользователь рисует область поиска на
карте, задаёт фильтры по жилью и цене, просматривает подходящие объявления и
экспортирует текущие результаты.

## Технологический стек

- Python
- FastAPI
- Pydantic
- Jinja2
- Leaflet
- OpenStreetMap
- OpenPyXL
- Pytest

## Возможности

- Рисование произвольного полигона поиска на карте Москвы.
- Фильтрация по типу жилья, площади и максимальной цене аренды.
- Локальная проверка попадания координат объявления в нарисованный полигон.
- Сохранение объявлений без координат с отметкой о неподтверждённом
  местоположении.
- Сортировка подтверждённых объявлений внутри области и неподтверждённых
  объявлений по цене.
- Экспорт текущих отфильтрованных результатов в Excel и автономный HTML-файл.

## Текущий статус источников

- **Тестовые данные:** полностью работают. Это встроенный набор объявлений для
  проверки интерфейса и фильтрации.
- **ЦИАН:** экспериментальный источник. При живом публичном запросе может быть
  показана Smart CAPTCHA, поэтому получение реальных объявлений не гарантировано.
- **Яндекс Недвижимость:** экспериментальный источник. Живой запрос может
  вернуть SSR-страницу или страницу с meta refresh без карточек объявлений,
  поэтому получение реальных объявлений не гарантировано.

## Структура проекта

```text
app/
  main.py                 FastAPI-приложение и HTTP-эндпоинты
  models/                 Модель объявления
  services/               Поиск, фильтрация и экспорт
  sources/                Адаптеры test, CIAN и Yandex Realty
  static/                 CSS и JavaScript интерфейса
  templates/              Jinja2-шаблоны
tests/
  fixtures/               Сохранённые HTML-fixtures для тестов парсеров
  test_*.py               Автоматические тесты
requirements.txt          Зависимости приложения и тестов
run.ps1                   Скрипт запуска для Windows
```

## Требования

- Windows 10/11 с PowerShell.
- Python 3.10 или новее, доступный как `python` или через Python Launcher
  (`py`).
- Подключение к интернету для первой установки зависимостей, карты и живых
  запросов к источникам.

## Установка и запуск

Откройте PowerShell в каталоге проекта и выполните:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

`run.ps1` создаёт `.venv`, если окружение отсутствует, устанавливает нужные
зависимости, выбирает следующий свободный порт при необходимости и выводит
локальный URL. Чтобы начать с другого предпочтительного порта:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Port 8004
```

Чтобы остановить сервер, нажмите `Ctrl+C` в том же окне PowerShell.

## Использование

1. Выберите источник и задайте тип жилья, площадь и максимальную цену.
2. Нарисуйте область поиска на карте.
3. Нажмите кнопку поиска.
4. Просмотрите таблицу результатов и при необходимости скройте объявления с
   неподтверждённым местоположением.

## Экспорт

После успешного поиска можно скачать текущую отфильтрованную таблицу в Excel
или автономный HTML. Файлы создаются в `output/`; в имени указаны дата и время
создания. Excel содержит форматированные заголовки, фильтры, закреплённую
первую строку и кликабельные ссылки. HTML открывается без запущенного
локального сервера.

## Поддерживаемые источники

- Тестовые данные
- ЦИАН (экспериментальный)
- Яндекс Недвижимость (экспериментальный)

## Известные ограничения

- Приложение не обходит CAPTCHA, не использует прокси, авторизацию, cookies
  или приватные API.
- Внешние сайты могут блокировать запросы, менять HTML или загружать карточки
  только JavaScript-ом после открытия страницы.
- Объявления без координат остаются в результатах, но их местоположение не
  подтверждено и не может быть проверено полигоном.
- Результаты последнего поиска хранятся только в памяти текущего процесса и
  исчезают после перезапуска приложения.
- Leaflet и OpenStreetMap требуют интернет-соединения.

## Тесты

После установки зависимостей выполните:

```powershell
.\.venv\Scripts\python.exe -m pytest
```
