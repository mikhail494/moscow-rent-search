(() => {
    const defaultMaxPrice = "90000";
    const status = document.querySelector("[data-draw-status]");
    const maxPriceInput = document.querySelector("#max-price");
    const filtersForm = document.querySelector("#filters-form");
    const searchButton = document.querySelector("#search-button");
    const saveSavedSearchButton = document.querySelector("#save-saved-search-button");
    const resultsSection = document.querySelector("[data-search-results]");
    const foundCount = document.querySelector("[data-found-count]");
    const listingsBody = document.querySelector("[data-listings-body]");
    const searchProgress = document.querySelector("[data-search-progress]");
    const showUnverifiedInput = document.querySelector("#show-unverified");
    const exportButtons = document.querySelectorAll("[data-export]");
    const sourceLabels = {
        test: "тестовые данные",
        cian: "ЦИАН",
        yandex_realty: "Яндекс Недвижимость",
    };
    let latestSearchData = null;

    if (!window.L || !window.L.Control || !window.L.Control.Draw) {
        if (status) {
            status.textContent = "Карта не загрузилась. Проверьте подключение к Leaflet.";
        }
        return;
    }

    const map = L.map("map", {
        zoomControl: true,
    }).setView([55.751244, 37.618423], 11);

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);

    const drawnItems = new L.FeatureGroup();
    map.addLayer(drawnItems);

    const drawControl = new L.Control.Draw({
        position: "topleft",
        draw: {
            marker: false,
            circlemarker: false,
            polyline: false,
            polygon: {
                allowIntersection: false,
                showArea: true,
                shapeOptions: {
                    color: "#176d5d",
                    weight: 3,
                },
            },
            rectangle: {
                shapeOptions: {
                    color: "#176d5d",
                    weight: 3,
                },
            },
            circle: false,
        },
        edit: {
            featureGroup: drawnItems,
            remove: true,
        },
    });

    map.addControl(drawControl);

    function applySavedSearchConfig(config) {
        if (!config?.polygon || config.polygon.geometry?.type !== "Polygon") {
            return;
        }

        document.querySelectorAll('input[name="flat_type"]').forEach((input) => {
            input.checked = config.property_types?.includes(input.value) || false;
        });

        for (const [selector, value] of [
            ["#min-area", config.min_area],
            ["#max-area", config.max_area],
            ["#max-price", config.max_price],
        ]) {
            const input = document.querySelector(selector);
            if (input && value !== null && value !== undefined) {
                input.value = value;
            }
        }

        if (showUnverifiedInput && typeof config.include_unverified_locations === "boolean") {
            showUnverifiedInput.checked = config.include_unverified_locations;
        }

        const savedArea = L.geoJSON(config.polygon);
        const savedLayers = savedArea.getLayers();
        if (savedLayers.length === 0) {
            return;
        }

        drawnItems.clearLayers();
        savedLayers.forEach((layer) => drawnItems.addLayer(layer));
        map.fitBounds(savedArea.getBounds(), { padding: [24, 24] });
        status?.classList.remove("is-error");
        if (status) {
            status.textContent = "Сохранённый район поиска загружен.";
        }
    }

    applySavedSearchConfig(window.savedSearchConfig);

    map.on(L.Draw.Event.CREATED, (event) => {
        drawnItems.clearLayers();
        drawnItems.addLayer(event.layer);
        if (status) {
            status.classList.remove("is-error");
            status.textContent = "Зона поиска выбрана на карте.";
        }
    });

    map.on(L.Draw.Event.DELETED, () => {
        if (status && drawnItems.getLayers().length === 0) {
            status.classList.remove("is-error");
            status.textContent = "Зона поиска не выбрана.";
        }
    });

    function showError(message) {
        if (status) {
            status.textContent = message;
            status.classList.add("is-error");
        }
    }

    function clearError() {
        status?.classList.remove("is-error");
    }

    function getNumberValue(selector) {
        const value = document.querySelector(selector)?.value.trim();
        return value ? Number(value) : null;
    }

    function formatPrice(price) {
        return `${new Intl.NumberFormat("ru-RU").format(price)} ₽`;
    }

    function formatArea(area) {
        return `${String(area).replace(".", ",")} м²`;
    }

    function propertyTypeLabel(propertyType) {
        return propertyType === "studio" ? "Студия" : "Однокомнатная";
    }

    function sourceLabel(source) {
        return sourceLabels[source] || source;
    }

    function createCell(value, className) {
        const cell = document.createElement("td");
        cell.textContent = value;
        if (className) {
            cell.className = className;
        }
        return cell;
    }

    function hideResults() {
        resultsSection?.setAttribute("hidden", "");
        listingsBody?.replaceChildren();
        setExportButtonsEnabled(false);
        if (foundCount) {
            foundCount.textContent = "";
        }
    }

    function displayedListings() {
        if (!latestSearchData) {
            return [];
        }
        return latestSearchData.listings.filter(
            (listing) => showUnverifiedInput?.checked || listing.location_verified,
        );
    }

    function setExportButtonsEnabled(enabled) {
        exportButtons.forEach((button) => {
            button.disabled = !enabled;
        });
    }

    function setProgressStep(stepName, text, state) {
        const step = searchProgress?.querySelector(`[data-progress-step="${stepName}"]`);
        if (!step) {
            return;
        }
        step.textContent = text;
        step.classList.remove("is-active", "is-complete", "is-error");
        if (state) {
            step.classList.add(`is-${state}`);
        }
    }

    function startProgress(source) {
        if (searchProgress) {
            searchProgress.hidden = false;
        }
        setProgressStep("source", `Источник запущен: ${sourceLabel(source)}.`, "active");
        setProgressStep("received", "Карточки ещё не получены", null);
        setProgressStep("filtered", "Фильтры ещё не применены", null);
        setProgressStep("completed", "Поиск выполняется", null);
    }

    function completeProgress(data) {
        setProgressStep("source", `Источник запущен: ${sourceLabel(data.source)}.`, "complete");
        setProgressStep("received", `Получено карточек: ${data.total_before_filtering}.`, "complete");
        setProgressStep("filtered", `Прошло фильтры: ${data.found_count}.`, "complete");
        setProgressStep("completed", "Поиск завершён.", "complete");
    }

    function showProgressError(message) {
        if (searchProgress) {
            searchProgress.hidden = false;
        }
        setProgressStep("completed", `Ошибка источника: ${message}`, "error");
    }

    function setSearching(isSearching) {
        if (!searchButton) {
            return;
        }
        searchButton.disabled = isSearching;
        searchButton.textContent = isSearching ? "Идёт поиск" : "Найти квартиры";
    }

    function setSavingSavedSearch(isSaving) {
        if (!saveSavedSearchButton) {
            return;
        }
        saveSavedSearchButton.disabled = isSaving;
        saveSavedSearchButton.textContent = isSaving
            ? "Сохранение..."
            : "Сохранить поиск";
    }

    function compareListings(first, second) {
        if (first.location_verified !== second.location_verified) {
            return first.location_verified ? -1 : 1;
        }
        return first.rent_price - second.rent_price;
    }

    function renderListings(data, shouldScroll = false) {
        if (!resultsSection || !foundCount || !listingsBody) {
            return;
        }

        const listings = displayedListings()
            .sort(compareListings);
        setExportButtonsEnabled(listings.length > 0);
        foundCount.textContent = `Показано: ${listings.length}; после фильтров: ${data.found_count} из ${data.total_before_filtering}`;
        listingsBody.replaceChildren();

        if (listings.length === 0) {
            const emptyRow = document.createElement("tr");
            const message = data.found_count === 0
                ? "По заданным фильтрам ничего не найдено."
                : "Нет объявлений с подтверждёнными координатами.";
            const emptyCell = createCell(message, "empty-results");
            emptyCell.colSpan = 9;
            emptyRow.append(emptyCell);
            listingsBody.append(emptyRow);
        }

        for (const listing of listings) {
            const row = document.createElement("tr");
            const locationStatus = listing.location_verified
                ? "В области, координаты подтверждены"
                : "Местоположение не подтверждено";
            const linkCell = document.createElement("td");
            const link = document.createElement("a");

            link.href = listing.url;
            link.target = "_blank";
            link.rel = "noreferrer";
            link.className = "listing-link";
            link.textContent = "Открыть";
            linkCell.append(link);

            row.append(
                createCell(formatPrice(listing.rent_price), "price-cell"),
                createCell(propertyTypeLabel(listing.property_type)),
                createCell(formatArea(listing.area_sqm)),
                createCell(listing.metro_station || "-"),
                createCell(listing.metro_minutes === null ? "-" : `${listing.metro_minutes} мин`),
                createCell(listing.address),
                createCell(
                    locationStatus,
                    listing.location_verified ? "location-status is-verified" : "location-status is-unverified",
                ),
                createCell(sourceLabel(listing.source)),
                linkCell,
            );
            listingsBody.append(row);
        }

        resultsSection.hidden = false;
        if (shouldScroll) {
            resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    filtersForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        clearError();
        hideResults();
        latestSearchData = null;

        const [searchArea] = drawnItems.getLayers();
        if (!searchArea) {
            showError("Сначала нарисуйте область поиска.");
            return;
        }

        const polygon = searchArea.toGeoJSON();
        if (polygon.geometry.type !== "Polygon") {
            showError("Выберите область в форме полигона или прямоугольника.");
            return;
        }

        const propertyTypes = Array.from(
            document.querySelectorAll('input[name="flat_type"]:checked'),
            (input) => input.value,
        );
        const payload = {
            source: document.querySelector('input[name="source"]:checked')?.value,
            polygon,
            property_types: propertyTypes,
            min_area: getNumberValue("#min-area"),
            max_area: getNumberValue("#max-area"),
            max_price: getNumberValue("#max-price"),
        };

        startProgress(payload.source);
        setSearching(true);
        try {
            const response = await fetch("/api/search", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await response.json();

            if (!response.ok || data.status !== "ok") {
                const message = data.detail?.[0]?.msg || "Не удалось обработать параметры поиска.";
                const errorMessage = data.error || message;
                showError(errorMessage);
                showProgressError(errorMessage);
                return;
            }

            latestSearchData = data;
            completeProgress(data);
            renderListings(data, true);
            if (status) {
                status.textContent = `Источник: ${sourceLabel(data.source)}. Найдено: ${data.found_count} из ${data.total_before_filtering}.`;
            }
        } catch (error) {
            const errorMessage = "Не удалось связаться с сервером.";
            showError(errorMessage);
            showProgressError(errorMessage);
        } finally {
            setSearching(false);
        }
    });

    saveSavedSearchButton?.addEventListener("click", async () => {
        clearError();
        const [searchArea] = drawnItems.getLayers();
        if (!searchArea) {
            showError("Сначала нарисуйте район поиска.");
            return;
        }

        const polygon = searchArea.toGeoJSON();
        if (polygon.geometry.type !== "Polygon") {
            showError("Выберите область в форме полигона или прямоугольника.");
            return;
        }

        const payload = {
            polygon,
            property_types: Array.from(
                document.querySelectorAll('input[name="flat_type"]:checked'),
                (input) => input.value,
            ),
            min_area: getNumberValue("#min-area"),
            max_area: getNumberValue("#max-area"),
            max_price: getNumberValue("#max-price"),
            include_unverified_locations: showUnverifiedInput?.checked || false,
        };

        setSavingSavedSearch(true);
        try {
            const response = await fetch("/api/saved-search", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await response.json();
            if (!response.ok) {
                const message = data.detail?.[0]?.msg || "Не удалось сохранить поиск.";
                showError(message);
                return;
            }

            if (status) {
                status.textContent = "Поиск сохранён.";
            }
        } catch (error) {
            showError("Не удалось сохранить поиск.");
        } finally {
            setSavingSavedSearch(false);
        }
    });

    showUnverifiedInput?.addEventListener("change", () => {
        if (latestSearchData) {
            renderListings(latestSearchData);
        }
    });

    function exportFilename(response, fallbackName) {
        const header = response.headers.get("Content-Disposition") || "";
        const match = header.match(/filename="?([^";]+)"?/i);
        return match?.[1] || fallbackName;
    }

    async function downloadExport(format) {
        if (displayedListings().length === 0) {
            setExportButtonsEnabled(false);
            return;
        }

        const includeUnverified = showUnverifiedInput?.checked ? "true" : "false";
        try {
            const response = await fetch(
                `/api/export/${format}?include_unverified=${includeUnverified}`,
            );
            if (!response.ok) {
                const data = await response.json();
                showError(data.detail || "Не удалось подготовить экспорт.");
                return;
            }

            const blob = await response.blob();
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = exportFilename(response, `rent_search.${format}`);
            document.body.append(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(link.href);
        } catch (error) {
            showError("Не удалось скачать файл экспорта.");
        }
    }

    exportButtons.forEach((button) => {
        button.addEventListener("click", () => downloadExport(button.dataset.export));
    });

    filtersForm?.addEventListener("reset", () => {
        window.setTimeout(() => {
            if (maxPriceInput) {
                maxPriceInput.value = defaultMaxPrice;
            }
            drawnItems.clearLayers();
            latestSearchData = null;
            hideResults();
            if (searchProgress) {
                searchProgress.hidden = true;
            }
            if (status) {
                clearError();
                status.textContent = "Зона поиска не выбрана.";
            }
        }, 0);
    });

    window.moscowRentSearch = {
        map,
        drawnItems,
        drawControl,
    };
})();
