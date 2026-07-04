document.addEventListener("DOMContentLoaded", () => {
    const triggers = document.querySelectorAll(".help-trigger");
    if (!triggers.length) {
        return;
    }

    const popover = document.createElement("aside");
    popover.className = "help-popover";
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-live", "polite");
    popover.hidden = true;

    const title = document.createElement("h3");
    const body = document.createElement("p");
    popover.append(title, body);
    document.body.appendChild(popover);

    function openPopover(event, trigger) {
        event.preventDefault();
        event.stopPropagation();

        title.textContent = trigger.dataset.helpTitle || "Help";
        body.textContent = trigger.dataset.helpBody || "";
        popover.hidden = false;

        const pointerX = event.clientX || trigger.getBoundingClientRect().right;
        const pointerY = event.clientY || trigger.getBoundingClientRect().bottom;
        positionPopover(pointerX, pointerY);
    }

    function positionPopover(x, y) {
        const margin = 12;
        const width = Math.min(320, window.innerWidth - margin * 2);
        popover.style.width = `${width}px`;

        const rect = popover.getBoundingClientRect();
        let left = x + margin;
        let top = y + margin;

        if (left + rect.width > window.innerWidth - margin) {
            left = Math.max(margin, x - rect.width - margin);
        }
        if (top + rect.height > window.innerHeight - margin) {
            top = Math.max(margin, y - rect.height - margin);
        }

        popover.style.left = `${left}px`;
        popover.style.top = `${top}px`;
    }

    function closePopover() {
        popover.hidden = true;
    }

    triggers.forEach((trigger) => {
        trigger.addEventListener("click", (event) => openPopover(event, trigger));
    });

    document.addEventListener("click", (event) => {
        if (!popover.hidden && !popover.contains(event.target)) {
            closePopover();
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closePopover();
        }
    });

    window.addEventListener("resize", closePopover);
    window.addEventListener("scroll", closePopover, true);

    // Configuration category tabs
    const configTabs = document.querySelectorAll("[data-config-tab]");
    const configSections = document.querySelectorAll("[data-config-category]");

    if (configTabs.length && configSections.length) {
        function showConfigCategory(category) {
            configSections.forEach((section) => {
                section.hidden = section.dataset.configCategory !== category;
            });
            configTabs.forEach((tab) => {
                tab.classList.toggle("active", tab.dataset.configTab === category);
            });
        }

        configTabs.forEach((tab) => {
            tab.addEventListener("click", () => showConfigCategory(tab.dataset.configTab || "overview"));
        });
        showConfigCategory("overview");
    }

    // ReRanking controls
    const rerankerModel = document.getElementById("rerankerModel");
    const rerankerApiKeyLabel = document.getElementById("rerankerApiKeyLabel");
    
    if (rerankerModel && rerankerApiKeyLabel) {
        function syncReRankerControls() {
            const model = rerankerModel.value;
            const isLocal = model.startsWith("local/");

            rerankerApiKeyLabel.style.display = isLocal ? "none" : "block";
        }
        
        rerankerModel.addEventListener("change", syncReRankerControls);
        syncReRankerControls();
    }

    const rerankerDiversityMode = document.getElementById("rerankerDiversityMode");
    const mmrSettings = document.querySelectorAll("[data-mmr-setting]");

    if (rerankerDiversityMode && mmrSettings.length) {
        function syncDiversityControls() {
            const showMmrSettings = rerankerDiversityMode.value === "mmr";
            mmrSettings.forEach((setting) => {
                setting.hidden = !showMmrSettings;
            });
        }

        rerankerDiversityMode.addEventListener("change", syncDiversityControls);
        syncDiversityControls();
    }

    // Voice provider controls
    const voiceProvider = document.getElementById("voiceProvider");
    const voiceBaseUrl = document.getElementById("voiceBaseUrl");
    const voiceRequiresApiKey = document.getElementById("voiceRequiresApiKey");
    const voiceSttModel = document.getElementById("voiceSttModel");
    const voiceSttLanguage = document.getElementById("voiceSttLanguage");
    const voiceTtsModel = document.getElementById("voiceTtsModel");
    const voiceDefaultVoice = document.getElementById("voiceDefaultVoice");
    const voiceFormat = document.getElementById("voiceFormat");

    if (voiceProvider) {
        function syncVoiceProviderFields() {
            const option = voiceProvider.selectedOptions[0];
            if (!option) {
                return;
            }
            if (voiceBaseUrl) {
                voiceBaseUrl.value = option.dataset.baseUrl || "";
            }
            if (voiceRequiresApiKey) {
                voiceRequiresApiKey.checked = option.dataset.requiresApiKey !== "0";
            }
            if (voiceSttModel) {
                voiceSttModel.value = option.dataset.sttModel || "";
            }
            if (voiceSttLanguage) {
                voiceSttLanguage.value = option.dataset.sttLanguage || "";
            }
            if (voiceTtsModel) {
                voiceTtsModel.value = option.dataset.ttsModel || "";
            }
            if (voiceDefaultVoice) {
                voiceDefaultVoice.value = option.dataset.voice || "alloy";
            }
            if (voiceFormat && option.dataset.format) {
                voiceFormat.value = option.dataset.format;
            }
        }

        voiceProvider.addEventListener("change", syncVoiceProviderFields);
    }

    // OCR provider controls
    const ocrProvider = document.getElementById("ocrProvider");
    const ocrBaseUrl = document.getElementById("ocrBaseUrl");
    const ocrRequiresApiKey = document.getElementById("ocrRequiresApiKey");
    const ocrDefaultModel = document.getElementById("ocrDefaultModel");
    const ocrMode = document.getElementById("ocrMode");
    const ocrOutputFormat = document.getElementById("ocrOutputFormat");
    const ocrInputTypes = document.querySelectorAll('input[name="ocr_input_types"]');

    if (ocrProvider) {
        function syncOcrProviderFields() {
            const option = ocrProvider.selectedOptions[0];
            if (!option) {
                return;
            }
            if (ocrBaseUrl) {
                ocrBaseUrl.value = option.dataset.baseUrl || "";
            }
            if (ocrRequiresApiKey) {
                ocrRequiresApiKey.checked = option.dataset.requiresApiKey !== "0";
            }
            if (ocrDefaultModel) {
                ocrDefaultModel.value = option.dataset.defaultModel || "";
            }
            if (ocrMode && option.dataset.ocrMode) {
                ocrMode.value = option.dataset.ocrMode;
            }
            if (ocrOutputFormat) {
                ocrOutputFormat.value = option.dataset.outputFormat || "text";
            }
            if (ocrInputTypes.length) {
                const selectedTypes = (option.dataset.inputTypes || "image,pdf").split(",");
                ocrInputTypes.forEach((checkbox) => {
                    checkbox.checked = selectedTypes.includes(checkbox.value);
                });
            }
        }

        ocrProvider.addEventListener("change", syncOcrProviderFields);
    }
});
