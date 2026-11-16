// i18n-fix-2026-10: Export-Ersatzwerte auf Englisch umgestellt (apply_interact_i18n.py)
document.addEventListener('DOMContentLoaded', () => {
    console.log("DOM geladen. Initialisiere App (Vollständig V6 - Debugging V3)...");

    // --- Referenzen zu DOM-Elementen ---
    const canvas = document.getElementById('imageCanvas');
    const ctx = canvas ? canvas.getContext('2d') : null;
    const previewCanvas = document.getElementById('previewCanvas');
    const previewCtx = previewCanvas ? previewCanvas.getContext('2d') : null;
    const canvasContainer = document.getElementById('canvas-container');
    const loadingSpinner = document.getElementById('loading-spinner');
    const imageInfo = document.getElementById('image-info');
    const filenameSpan = document.getElementById('filename');
    const statusMessage = document.getElementById('status-message');
    const finishedMessageDiv = document.getElementById('finished-message');
    const progressContainer = document.querySelector('.progress-container');
    const progressBar = document.getElementById('progress-bar');
    const progressLabel = document.getElementById('progress-label');
    const imageListUl = document.getElementById('image-list');
    const imageListContainer = document.getElementById('image-list-container');
    const sliceSliderContainer = document.getElementById('slice-slider-container');
    const sliceSlider = document.getElementById('slice-slider');
    const currentSliceDisplay = document.getElementById('current-slice-display');
    const totalSlicesDisplay = document.getElementById('total-slices-display');
    const currentAxisDisplay = document.getElementById('current-axis-display');
    const axisSelectorContainer = document.getElementById('axis-selector-container');
    const btnAxisAxial = document.getElementById('btn-axis-axial');
    const btnAxisSagittal = document.getElementById('btn-axis-sagittal');
    const btnAxisCoronal = document.getElementById('btn-axis-coronal');
    const axisButtons = [btnAxisAxial, btnAxisSagittal, btnAxisCoronal];
    const previewAreaContainer = document.getElementById('preview-area-container');
    const preview3dArea = document.getElementById('preview-3d-area');
    const placeholderText3d = preview3dArea ? preview3dArea.querySelector('.placeholder-text-3d') : null;
    const niftiViewerCanvas = document.getElementById('nifti-3d-viewer-canvas');
    const resultActionsDiv = document.getElementById('result-actions');
    const btnDownloadResults = document.getElementById('btn-download-results');
    const btnRetrySegmentation = document.getElementById('btn-retry-segmentation');
    const btnNewSegmentationLink = document.getElementById('btn-new-segmentation-link');
    const controlsWLDiv = document.getElementById('controls-wl');
    const methodSelectionDiv = document.getElementById('method-selection');
    const controlsAnnotateSamDiv = document.getElementById('controls-annotate-sam');
    const controlsThresholdDiv = document.getElementById('controls-threshold');
    const sliderWL = document.getElementById('slider-wl');
    const inputWL = document.getElementById('input-wl');
    const sliderWW = document.getElementById('slider-ww');
    const inputWW = document.getElementById('input-ww');
    const btnResetWLWW = document.getElementById('btn-reset-wlww');
    const presetButtons = document.querySelectorAll('.preset-btn');
    const dicomControlsDiv = document.querySelector('.dicom-controls');
    const dicomNavControlsWL = document.getElementById('dicom-nav-controls-wl');
    const btnPreviousWl = document.getElementById('btn-previous-wl');
    const btnNextWl = document.getElementById('btn-next-wl');
    const btnApplyWl = document.getElementById('btn-apply-wl');
    const btnSelectSam = document.getElementById('btn-select-sam');
    const btnSelectThresh = document.getElementById('btn-select-thresh');
    const btnBackToWlFromSelect = document.getElementById('btn-back-to-wl-from-select');
    const fixedWlDisplaySam = document.getElementById('fixed-wl-display-sam');
    const fixedWwDisplaySam = document.getElementById('fixed-ww-display-sam');
    const btnBackToMethodSelectFromSam = document.getElementById('btn-back-to-method-select-from-sam');
    const btnBox = document.getElementById('btn-box');
    const btnPositive = document.getElementById('btn-positive');
    const btnNegative = document.getElementById('btn-negative');
    const btnFreehand = document.getElementById('btn-freehand'); // NEU
    const btnUndo = document.getElementById('btn-undo');
    const btnReset = document.getElementById('btn-reset');
    const btnMarkOthersEmptySam = document.getElementById('btn-mark-others-empty-sam');
    const toolButtonsSAM = [btnBox, btnPositive, btnNegative, btnFreehand]; // NEU: btnFreehand hinzugefügt
    const btnSaveTemplate = document.getElementById('btn-save-template');
    const btnApplyTemplate = document.getElementById('btn-apply-template');
    const btnApplyTemplateMulti = document.getElementById('btn-apply-template-multi');
    const templateSelectionPanel = document.getElementById('template-selection-panel');
    const templateImageListDiv = document.getElementById('template-image-list');
    const btnConfirmTemplateApply = document.getElementById('btn-confirm-template-apply');
    const panelSelectAllLink = document.getElementById('panel-select-all');
    const panelDeselectAllLink = document.getElementById('panel-deselect-all');
    const dicomNavControlsSAM = document.getElementById('dicom-nav-controls-sam');
    const btnPreviousSam = document.getElementById('btn-previous-sam');
    const btnSegmentSam = document.getElementById('btn-segment-sam');
    const btnNextSam = document.getElementById('btn-next-sam');
    const btnViewOriginal = document.getElementById('btn-view-original');
    const btnViewSegmentation = document.getElementById('btn-view-segmentation');
    const viewToggleContainer = document.getElementById('view-toggle-container');
    const btnFinalizeSam = document.getElementById('btn-finalize-sam');
    const cbFillHolesSam = document.getElementById('cb-fill-holes-sam');
    const cbRemoveOutliersSam = document.getElementById('cb-remove-outliers-sam');
    const inputMinOutlierSizeSam = document.getElementById('input-min-outlier-size-sam');
    const fixedWlDisplayThresh = document.getElementById('fixed-wl-display-thresh');
    const fixedWwDisplayThresh = document.getElementById('fixed-ww-display-thresh');
    const btnBackToMethodSelectFromThresh = document.getElementById('btn-back-to-method-select-from-thresh');
    const sliderThreshLower = document.getElementById('slider-thresh-lower');
    const inputThreshLower = document.getElementById('input-thresh-lower');
    const displayThreshLower = document.getElementById('display-thresh-lower');
    const sliderThreshUpper = document.getElementById('slider-thresh-upper');
    const inputThreshUpper = document.getElementById('input-thresh-upper');
    const displayThreshUpper = document.getElementById('display-thresh-upper');
    const btnResetThresh = document.getElementById('btn-reset-thresh');
    const btnFinalizeThresh = document.getElementById('btn-finalize-thresh');
    const thresholdControlsDiv = document.getElementById('controls-threshold')?.querySelector('.threshold-controls');
    const cbFillHolesThresh = document.getElementById('cb-fill-holes-thresh');
    const cbRemoveOutliersThresh = document.getElementById('cb-remove-outliers-thresh');
    const inputMinOutlierSizeThresh = document.getElementById('input-min-outlier-size-thresh');
    const inputMaterialDensitySam = document.getElementById('input-material-density-sam');
    const inputMaterialDensityThresh = document.getElementById('input-material-density-thresh');
    const templateSliceFromInput = document.getElementById('template-slice-from');
    const templateSliceToInput = document.getElementById('template-slice-to');
    const btnSelectRangeTemplate = document.getElementById('btn-select-range-template');
    const templateAxisInfoSpan = document.getElementById('template-axis-info'); // Für die Achseninfo im Panel
    const jsonUploadInput = document.getElementById('json-upload-input');
    const groundTruthUploadInput = document.getElementById('ground-truth-upload-input');
    const metricsDisplayDiv = document.getElementById('comparison-metrics-display');
    const diceScoreDisplay = document.getElementById('dice-score-display');
    const hausdorffDistanceDisplay = document.getElementById('hausdorff-distance-display');
    const gtFileInput = document.getElementById('gt-file-input');
    const gtFileNameDisplay = document.getElementById('gt-file-name-display');
    const cbShowVoxelSizeSam = document.getElementById('cb-show-voxel-size-sam');
    const cbShowVoxelSizeThresh = document.getElementById('cb-show-voxel-size-thresh');
    
    // --- Hybrid-UI Referenzen ---
    const btnRefineWithThreshold = document.getElementById('btn-refine-with-threshold');
    const hybridRefinementContainer = document.getElementById('hybrid-refinement-container');
    const hybridSliderThreshLower = document.getElementById('hybrid-slider-thresh-lower');
    const hybridInputThreshLower = document.getElementById('hybrid-input-thresh-lower');
    const hybridDisplayThreshLower = document.getElementById('hybrid-display-thresh-lower');
    const hybridSliderThreshUpper = document.getElementById('hybrid-slider-thresh-upper');
    const hybridInputThreshUpper = document.getElementById('hybrid-input-thresh-upper');
    const hybridDisplayThreshUpper = document.getElementById('hybrid-display-thresh-upper');
    const btnApplyHybridRefinement = document.getElementById('btn-apply-hybrid-refinement');
    const btnCancelHybridRefinement = document.getElementById('btn-cancel-hybrid-refinement');
    
    // --- Few-Shot Threshold Referenz (vereinfacht) ---
    const cbFewShotThresholdEnabled = document.getElementById('cb-fewshot-threshold-enabled');

    // (finale 2D-Viewer-Referenzen werden weiter unten einmalig deklariert)




    // --- Zustandvariablen ---
    let currentImage = new Image();
    let currentRawPixelData = null;
    let currentRawDataType = null;
    let originalWidth = 0, originalHeight = 0;
    let currentDisplayedFilename = '';
    let currentIndex = 0;
    let debugMode = true; // Debug-Modus für Mauskoordinaten-Logging (aktiviert für Fehlerbehebung)
    // finalOverlayFiles wird weiter unten einmalig verwaltet

    let fileType = 'dicom_series'; // Wird in initializeApp überschrieben
    let sliceCounts = {'axial': 0, 'sagittal': 0, 'coronal': 0};
    let dicomFilenames = [];
    let baseVolumeFilename = "volume_data"; // Default

    let totalSlicesForCurrentAxis = 0;
    let currentAxis = 'axial';
    let sam2FixatedAxis = null;
    let currentWorkflowStage = 'wl_select';
    let segmentationMethod = null;
    let savedMethodPerSlice = {};
    let currentWL = null, currentWW = null;
    let fixedWL = null, fixedWW = null;
    let annotations = [];
    let freehandPath = []; // NEU: Array zum Speichern des Freihand-Pfades
    let allAnnotations = {};
    // Zuletzt bei einem Few-Shot-/S1SFQ-/Memory-Run verwendete Support-Masken.
    // Wird beim Abschluss (triggerFinishSequence) an den Server geschickt, damit in
    // segmentation_parameters.json nur die Koordinaten der tatsächlich genutzten
    // Support-Slices landen. null = kein Few-Shot benutzt (keine Filterung).
    let lastSupportMaskFiles = null;
    let currentSegmentationUrl = null; // URL der aktuellen Segmentierung
    let isShowingSegmentation = false; // Ob gerade Segmentierung angezeigt wird
    let currentTool = null;
    let isDrawing = false, startX, startY, currentX, currentY;
    let savedAnnotationTemplate = null;
    let currentLowerThresh = -500, currentUpperThresh = 500;
    let thresholdMaskData = null;
    let scale = 1.0, originX = 0, originY = 0;
    let isPanning = false, panStartX, panStartY;
    const ZOOM_FACTOR = 1.2, MIN_ZOOM = 0.2, MAX_ZOOM = 5.0;
    let settingSlidersProgrammatically = false;
    let savedIndicesPerAxis = {};
    let niivueViewer = null;
    let offscreenMaskCanvas = null, offscreenMaskCtx = null;
    let debugClickPos = null; // Für Maus-Versatz Debugging
    let batchSegmentationActive = false;
    let voxelSizePreview = { show: false, size: 0 }; // NEU: Zustand für die Voxel-Vorschau
    let finalOverlaySource = 'manual'; // Mögliche Werte: 'manual', 'few_shot', 'memory', 's1sfq'
    
    // --- Hybrid-Ansatz Variablen (SAM + Thresholding) ---
    let currentSamMask = null; // Aktuelle SAM-Maske für Verfeinerung
    let refinementMode = false; // Flag für Verfeinerungs-Modus
    let hybridThresholdLower = -500; // Standard-Threshold für Verfeinerung
    let hybridThresholdUpper = 500;
    let fewShotThresholdEnabled = false; // Flag für Few-Shot mit Threshold (verwendet hybridThreshold-Werte)
    // Globaler HU-Bereich des gesamten Volumens (vom Backend) für die Verfeinerungs-Regler
    let globalDataMin = null;
    let globalDataMax = null;

    // --- Slice-Range (optional, für SAM und Threshold) ---
    let sliceRangeStart = null;
    let sliceRangeEnd = null;

    function syncSliceRangeInputs() {
        document.querySelectorAll('.slice-range-start').forEach(inp => {
            inp.value = (sliceRangeStart != null) ? sliceRangeStart : '';
        });
        document.querySelectorAll('.slice-range-end').forEach(inp => {
            inp.value = (sliceRangeEnd != null) ? sliceRangeEnd : '';
        });
    }

    function attachSliceRangeHandlers() {
        document.querySelectorAll('.slice-range-start').forEach(inp => {
            inp.addEventListener('change', () => {
                const v = inp.value.trim();
                sliceRangeStart = (v === '') ? null : parseInt(v);
                if (isNaN(sliceRangeStart)) sliceRangeStart = null;
                syncSliceRangeInputs();
            });
        });
        document.querySelectorAll('.slice-range-end').forEach(inp => {
            inp.addEventListener('change', () => {
                const v = inp.value.trim();
                sliceRangeEnd = (v === '') ? null : parseInt(v);
                if (isNaN(sliceRangeEnd)) sliceRangeEnd = null;
                syncSliceRangeInputs();
            });
        });
        document.querySelectorAll('.slice-range-reset').forEach(btn => {
            btn.addEventListener('click', () => {
                sliceRangeStart = null;
                sliceRangeEnd = null;
                syncSliceRangeInputs();
            });
        });
    }

    function addSliceRangeToPayload(payload) {
        if (sliceRangeStart != null) payload.slice_start = sliceRangeStart;
        if (sliceRangeEnd != null) payload.slice_end = sliceRangeEnd;
        return payload;
    }

    // --- Hilfsfunktionen ---

    // Schreibt in die Statuszeile unter den Steuerelementen. Der uebrige Code
    // setzt statusMessage.textContent direkt; ein paar Stellen riefen aber
    // schon immer diese Funktion auf, ohne dass es sie gab.
    // Hinweis: in der Stufe 'finished' blendet updateUIForStage() die Zeile
    // aus - dort ist der Auswahl-Dialog die sichtbare Rueckmeldung.
    function updateStatusMessage(text) {
        if (statusMessage) statusMessage.textContent = text;
    }

    function debounce(func, wait) { let timeout; return function executedFunction(...args) { const later = () => { clearTimeout(timeout); func(...args); }; clearTimeout(timeout); timeout = setTimeout(later, wait); }; }

    // NEUE FUNKTIONEN FÜR BLOCKIERENDES OVERLAY
    let currentAbortController = null;   // aktiver Abbruch-Controller (nur bei cancellable-Loadern)

    function showBlockingLoader(message = "Verarbeite...", options = {}) {
        const loader = document.getElementById('blocking-loader');
        const loaderMessage = document.getElementById('loader-message');
        const cancelBtn = document.getElementById('loader-cancel-btn');
        if (loader && loaderMessage) {
            loaderMessage.textContent = message;
            loader.classList.remove('hidden');
        }
        // Abbruch-Möglichkeit: nur wenn der Aufrufer sie explizit anbietet (und den signal weiterreicht)
        currentAbortController = options.cancellable ? new AbortController() : null;
        if (cancelBtn) {
            cancelBtn.classList.toggle('hidden', !options.cancellable);
            cancelBtn.onclick = options.cancellable ? () => {
                if (currentAbortController) currentAbortController.abort();
                hideBlockingLoader();
            } : null;
        }
    }

    // (veraltete finale Overlay-Viewer-Funktionen entfernt; aktuelle Implementierung steht weiter unten)

    // Neue Hilfsfunktion zum Zurücksetzen der Backend-Session
    async function resetSegmentationSession() {
        try {
            // Neue Archive-Sitzung erstellen (alter Ordner bleibt erhalten)
            const archiveResp = await fetch('/back_to_annotation', { method: 'POST' });
            const archiveResult = await archiveResp.json();
            if (!archiveResult.success) {
                console.error('Fehler beim Erstellen neuer Archive-Sitzung:', archiveResult.error);
            } else {
                console.log('Neue Archive-Sitzung erstellt:', archiveResult.archive_folder);
            }

            // Segmentierungsmethode zurücksetzen
            const response = await fetch('/set_method', { method: 'POST' });
            const result = await response.json();
            if (!result.success) {
                console.error('Fehler beim Zurücksetzen der Server-Session:', result.error);
            }
        } catch (error) {
            console.error('Netzwerkfehler beim Zurücksetzen der Server-Session:', error);
        }
    }

    function hideBlockingLoader() {
        const loader = document.getElementById('blocking-loader');
        if (loader) {
            loader.classList.add('hidden');
        }
        const cancelBtn = document.getElementById('loader-cancel-btn');
        if (cancelBtn) { cancelBtn.classList.add('hidden'); cancelBtn.onclick = null; }
        currentAbortController = null;
    }

    function showConfirmationModal(title, text) {
        return new Promise((resolve) => {
            const overlay = document.getElementById('confirmation-modal-overlay');
            const titleEl = document.getElementById('confirmation-modal-title');
            const textEl = document.getElementById('confirmation-modal-text');
            const confirmBtn = document.getElementById('confirmation-modal-confirm');
            const cancelBtn = document.getElementById('confirmation-modal-cancel');
            const closeBtn = document.getElementById('confirmation-modal-close');

            if (!overlay) {
                console.error("Modal-Elemente nicht im DOM gefunden!");
                resolve(false); // Fallback auf 'Abbrechen'
                return;
            }

            titleEl.textContent = title;
            textEl.textContent = text;

            overlay.classList.remove('hidden');

            const close = (decision) => {
                overlay.classList.add('hidden');
                confirmBtn.onclick = null;
                cancelBtn.onclick = null;
                closeBtn.onclick = null;
                overlay.onclick = null;
                resolve(decision);
            };

            confirmBtn.onclick = () => close(true);
            cancelBtn.onclick = () => close(false);
            closeBtn.onclick = () => close(false);
            
            overlay.onclick = (e) => {
                if (e.target === overlay) {
                    close(false);
                }
            };
        });
    }

    // Fragt beim GT-Vergleich den Modus ab: 'fast' (grob/schnell) oder 'accurate' (voll/genau) bzw. null (Abbruch).
    function askComparisonMode() {
        return new Promise((resolve) => {
            const old = document.getElementById('compare-mode-overlay');
            if (old) old.remove();
            const overlay = document.createElement('div');
            overlay.id = 'compare-mode-overlay';
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">
                        <h3>Vergleichsmodus wählen</h3>
                        <button class="close-modal" id="compare-mode-close">&times;</button>
                    </div>
                    <div class="modal-body">
                        <p>Wie soll der Ground-Truth-Vergleich laufen? Schnell ist grob (heruntergerechnete Roll-Suche), Genau nutzt die volle Auflösung.</p>
                    </div>
                    <div class="modal-footer" style="gap:8px;">
                        <button class="modal-btn secondary" id="compare-mode-fast">Schnell (grob, ~30 s)</button>
                        <button class="modal-btn primary" id="compare-mode-accurate">Genau (Standard, ~2 min)</button>
                    </div>
                </div>`;
            document.body.appendChild(overlay);
            const close = (decision) => { overlay.remove(); resolve(decision); };
            document.getElementById('compare-mode-fast').onclick = () => close('fast');
            document.getElementById('compare-mode-accurate').onclick = () => close('accurate');
            document.getElementById('compare-mode-close').onclick = () => close(null);
            overlay.onclick = (e) => { if (e.target === overlay) close(null); };
        });
    }

    function drawVoxelSizePreviewOnContext(context) {
    if (!context || !voxelSizePreview.show || voxelSizePreview.size <= 0) return;

    const size = voxelSizePreview.size;
    // Wir positionieren die Vorschau oben rechts im Bild (nicht im Fenster),
    // damit sie mit dem Bild zoomt und verschoben wird, was den Größenvergleich erleichtert.
    const margin = 10 / scale; // 10px Rand, skaliert mit dem Zoom
    const x = originalWidth - size - margin;
    const y = margin;

    // Stil für das Quadrat
    context.fillStyle = 'rgba(255, 255, 0, 0.7)'; // Halbtransparentes Gelb
    context.strokeStyle = 'rgba(220, 50, 50, 1)';   // Roter Rand
    context.lineWidth = Math.max(1, 2 / scale);   // Randdicke passt sich dem Zoom an

    // Quadrat zeichnen
    context.fillRect(x, y, size, size);
    context.strokeRect(x, y, size, size);

    // Text für die Seitenlänge hinzufügen
    context.fillStyle = 'white';
    context.font = `${Math.max(10, 14 / scale)}px Sans-Serif`;
    context.textAlign = 'right';
    context.textBaseline = 'bottom';
    const text = `${size.toFixed(1)} px`;
    // Text unterhalb des Quadrats positionieren
    context.fillText(text, x + size, y - (2 / scale));
    }

    function showSimpleAlert(title, text) {
    return new Promise((resolve) => {
        const overlay = document.getElementById('confirmation-modal-overlay');
        const titleEl = document.getElementById('confirmation-modal-title');
        const textEl = document.getElementById('confirmation-modal-text');
        const confirmBtn = document.getElementById('confirmation-modal-confirm');
        const cancelBtn = document.getElementById('confirmation-modal-cancel');
        const closeBtn = document.getElementById('confirmation-modal-close');

        if (!overlay) {
            console.error("Modal-Elemente nicht gefunden!");
            resolve();
            return;
        }

        titleEl.textContent = title;
        textEl.innerHTML = text.replace(/\n/g, '<br>'); // Ersetzt Zeilenumbrüche mit <br>
        confirmBtn.textContent = 'Okay';
        cancelBtn.style.display = 'none'; // Verstecke den Abbrechen-Button
        closeBtn.style.display = 'none';  // Verstecke das 'x'

        overlay.classList.remove('hidden');

        const close = () => {
            overlay.classList.add('hidden');
            confirmBtn.onclick = null;

            // Setze die Buttons für die nächste Nutzung zurück
            confirmBtn.textContent = 'Bestätigen';
            cancelBtn.style.display = '';
            closeBtn.style.display = '';
            resolve();
        };

        confirmBtn.onclick = close;
    });
}

    // NEUE HELFER-FUNKTIONEN FÜR LADEANZEIGEN
    function showProgress(label, isIndeterminate = false) {
        if (progressContainer && progressBar && progressLabel) {
            progressLabel.textContent = label;
            if (isIndeterminate) {
                progressBar.removeAttribute('value'); // Aktiviert den unbestimmten Modus
            } else {
                progressBar.value = 0;
                progressBar.max = 100;
            }
            progressContainer.classList.remove('hidden');
            showLoading(false); // Verstecke den Haupt-Spinner, um doppelte Anzeigen zu vermeiden
        }
    }

    function updateProgress(value, total) {
        if (progressBar && progressLabel) {
            if (progressBar.hasAttribute('value')) { // Nur updaten, wenn nicht unbestimmt
                progressBar.value = value;
                progressBar.max = total;
                progressLabel.textContent = `Verarbeite... ${value} / ${total}`;
            }
        }
    }

    function hideProgress() {
        if (progressContainer) {
            setTimeout(() => { // Kleine Verzögerung, damit der 100%-Status sichtbar ist
                progressContainer.classList.add('hidden');
            }, 1500);
        }
    }
    // --- Ende der neuen Funktionen ---

    // *** KORRIGIERTE isVolumetric Funktion ***
    function isVolumetric() {
        let cleanFileType = fileType;
        if (typeof fileType === 'string' && fileType.length >= 2 && fileType.startsWith('"') && fileType.endsWith('"')) {
            cleanFileType = fileType.substring(1, fileType.length - 1);
        }
        // console.log("[DEBUG isVolumetric] Ursprüngliches fileType:", fileType, "Bereinigtes cleanFileType:", cleanFileType);
        // console.log("[DEBUG isVolumetric] Vergleich cleanFileType === 'nrrd':", cleanFileType === 'nrrd');
        // console.log("[DEBUG isVolumetric] Vergleich cleanFileType === 'nifti':", cleanFileType === 'nifti');
        let result = cleanFileType === 'nrrd' || cleanFileType === 'nifti';
        // console.log("[DEBUG isVolumetric] Ergebnis:", result);
        return result;
    }

    function clearCanvas() { if (ctx && canvas) { try {ctx.save(); ctx.setTransform(1,0,0,1,0,0); ctx.clearRect(0,0,canvas.width,canvas.height); ctx.fillStyle = '#ddd'; ctx.fillRect(0,0,canvas.width,canvas.height); ctx.restore(); } catch(e){console.error("Clear canvas error",e)} }}
    function clearPreviewCanvas() { if (previewCtx && previewCanvas) { try {previewCtx.save(); previewCtx.setTransform(1,0,0,1,0,0); previewCtx.clearRect(0,0,previewCanvas.width,previewCanvas.height); previewCtx.restore(); } catch(e){console.error("Clear preview error",e)} }}
    function showLoading(show) { if (loadingSpinner) loadingSpinner.style.display = show ? 'block' : 'none'; }
    function resetZoomAndPan() { scale = 1.0; originX = 0; originY = 0; setTool(null); isDrawing = false; startX = undefined; debugClickPos = null; isPanning = false; setCanvasCursor(); }
    function resetSamAnnotationsAndZoom() { annotations = []; if(btnUndo) btnUndo.disabled = true; resetZoomAndPan(); }

    function displayComparisonMetrics({ dice, assd, volume_diff, sensitivity_percent, specificity_percent, precision_percent }) {
        if (!metricsDisplayDiv || !diceScoreDisplay ) return;

        const volumeDiffDisplay = document.getElementById('volume-diff-display');
        const assdDisplay = document.getElementById('assd-display');
        const sensitivityDisplay = document.getElementById('sensitivity-display');
        const specificityDisplay = document.getElementById('specificity-display');
        const precisionDisplay = document.getElementById('precision-display');
        diceScoreDisplay.textContent = dice.toFixed(4);

        if (assdDisplay) {
            assdDisplay.textContent = assd.toFixed(2) + ' mm';
        }

        if (volumeDiffDisplay) {
            if (volume_diff != null) {
                // Fügt ein "+" für positive Werte hinzu für bessere Lesbarkeit
                const sign = volume_diff > 0 ? '+' : '';
                volumeDiffDisplay.textContent = `${sign}${volume_diff.toFixed(2)} %`;
            } else {
                volumeDiffDisplay.textContent = "-";
            }
        }

        if (sensitivityDisplay && sensitivity_percent != null) {
        sensitivityDisplay.textContent = sensitivity_percent.toFixed(2) + ' %';
        }
        if (specificityDisplay && specificity_percent != null) {
        specificityDisplay.textContent = specificity_percent.toFixed(2) + ' %';
        }
        if (precisionDisplay && precision_percent != null) {
        precisionDisplay.textContent = precision_percent.toFixed(2) + ' %';
        }
        metricsDisplayDiv.classList.remove('hidden');

        // WICHTIG: Die Info-Icons neu initialisieren, falls sie dynamisch hinzugefügt wurden
        // Da sie jetzt statisch in der HTML sind, sollte der initiale Aufruf reichen.
        // Ein erneuter Aufruf hier schadet aber nicht und macht es robuster.
        initializeInfoIcons();
    }

    // === Finale 2D/3D Toggle ===
    const finalViewToggleContainer = document.getElementById('final-view-toggle-container');
    const btnFinalView3D = document.getElementById('btn-final-view-3d');
    const btnFinalView2D = document.getElementById('btn-final-view-2d');
    // Finale 2D-Viewer (einmalige Referenzen)
    const final2dViewerContainer = document.getElementById('final-2d-viewer-container');
    const finalSliceSlider = document.getElementById('final-slice-slider');
    const finalCurrentSliceDisplay = document.getElementById('final-current-slice-display');
    const finalTotalSlicesDisplay = document.getElementById('final-total-slices-display');
    const finalOverlayCanvas = document.getElementById('final-overlay-canvas');
    const finalOverlayCtx = finalOverlayCanvas ? finalOverlayCanvas.getContext('2d') : null;
    let finalOverlayFiles = [];

    function setFinalView(mode) {
        if (!previewAreaContainer) return;
        if (mode === '3d') {
            btnFinalView3D?.classList.add('active');
            btnFinalView2D?.classList.remove('active');
            preview3dArea?.classList.remove('hidden');
            final2dViewerContainer?.classList.add('hidden');
        } else {
            btnFinalView2D?.classList.add('active');
            btnFinalView3D?.classList.remove('active');
            preview3dArea?.classList.add('hidden');
            final2dViewerContainer?.classList.remove('hidden');
        }
        // Legende nur in der 3D-Ansicht (und nur bei aktivem GT-Vergleich)
        updateGtLegendVisibility();
    }

     function updateFinalSliceSliderLabel(index) {
        finalCurrentSliceDisplay && (finalCurrentSliceDisplay.textContent = index + 1);
        finalTotalSlicesDisplay && (finalTotalSlicesDisplay.textContent = finalOverlayFiles.length);
    }

    function setupFinalOverlayViewer() {
        if (!final2dViewerContainer || !finalSliceSlider || finalOverlayFiles.length === 0) return;
        finalSliceSlider.min = 0; finalSliceSlider.max = finalOverlayFiles.length - 1; finalSliceSlider.value = 0;
        loadFinalOverlayImage(0); updateFinalSliceSliderLabel(0);
    }

    function loadFinalOverlayImage(index) {
        if (!finalOverlayCtx || index < 0 || index >= finalOverlayFiles.length) return;
        const filename = finalOverlayFiles[index];

        // Dynamische URL basierend auf der vom Backend mitgeteilten Quelle
        let baseUrl;
        if (finalOverlaySource === 'memory') {
            baseUrl = '/overlays_memory_archive/';
        } else if (finalOverlaySource === 's1sfq') {
            baseUrl = '/overlays_s1sfq_archive/';
        } else if (finalOverlaySource === 'few_shot') {
            baseUrl = '/overlays_fewshot_archive/';
        } else { // 'manual' oder Fallback
            baseUrl = '/overlays_archive/';
        }

        const overlayUrl = `${baseUrl}${filename}?t=${Date.now()}`;

        // Extrahiere den Original-Slice-Dateinamen aus dem Overlay-Dateinamen
        // Overlay: "axial_slice_0050_overlay.png" -> Original: "axial_slice_0050.png"
        const originalFilename = filename.replace('_overlay.png', '.png');
        const originalUrl = `/slice/${currentAxis}/${index}?t=${Date.now()}`;

        console.log("Lade Original:", originalUrl, "und Overlay:", overlayUrl);

        // Lade zuerst das Original-Bild
        const originalImg = new Image();
        originalImg.onload = () => {
            if (!finalOverlayCanvas) return;

            // Setze Canvas-Größe auf Original-Bildgröße
            finalOverlayCanvas.width = originalImg.naturalWidth;
            finalOverlayCanvas.height = originalImg.naturalHeight;

            // 1. Zeichne das Original-Bild
            finalOverlayCtx.clearRect(0, 0, finalOverlayCanvas.width, finalOverlayCanvas.height);
            finalOverlayCtx.drawImage(originalImg, 0, 0);

            // 2. Lade und zeichne das Overlay transparent darüber
            const overlayImg = new Image();
            overlayImg.onload = () => {
                // Zeichne Overlay mit voller Transparenz (PNG Alpha wird respektiert)
                finalOverlayCtx.globalAlpha = 1.0;  // PNG hat bereits Alpha-Kanal
                finalOverlayCtx.drawImage(overlayImg, 0, 0);
                finalOverlayCtx.globalAlpha = 1.0;  // Reset
            };
            overlayImg.onerror = () => {
                console.error(`Konnte Overlay-Datei nicht laden: ${overlayUrl}`);
            };
            overlayImg.src = overlayUrl;
        };
        originalImg.onerror = () => {
            console.warn(`Konnte Original-Slice nicht laden: ${originalUrl}, zeige nur Overlay`);
            // Fallback: Zeige nur Overlay (wie vorher)
            const overlayImg = new Image();
            overlayImg.onload = () => {
                if (!finalOverlayCanvas) return;
                finalOverlayCanvas.width = overlayImg.naturalWidth;
                finalOverlayCanvas.height = overlayImg.naturalHeight;
                finalOverlayCtx.clearRect(0, 0, finalOverlayCanvas.width, finalOverlayCanvas.height);
                finalOverlayCtx.drawImage(overlayImg, 0, 0);
            };
            overlayImg.src = overlayUrl;
        };
        originalImg.src = originalUrl;
    }

    if (btnFinalView3D) {
        btnFinalView3D.addEventListener('click', () => setFinalView('3d'));
    }
    if (btnFinalView2D) {
        btnFinalView2D.addEventListener('click', () => setFinalView('2d'));
    }

// Legende (#gt-legend) NUR zeigen, wenn ein GT-Vergleich aktiv ist UND die 3D-Ansicht sichtbar ist.
function updateGtLegendVisibility() {
    const legend = document.getElementById('gt-legend');
    if (!legend) return;
    const area3d = document.getElementById('preview-3d-area');
    const in3d = !!area3d && !area3d.classList.contains('hidden');
    const show = (window.gtComparisonActive === true) && in3d;
    legend.classList.toggle('hidden', !show);
}

async function loadGroundTruthOverlay(overlayUrl) {
    if (!niivueViewer) {
        alert("3D-Viewer ist nicht initialisiert.");
        return;
    }
    if (!overlayUrl) {
        console.warn("[GT-Vergleich] Keine Vergleichs-Overlay-URL erhalten.");
        return;
    }
    showBlockingLoader("Lade Vergleichs-Ansicht...");
    try {
        // Eine EINZIGE Vergleichs-Maske mit drei Farben statt zwei überlagerter Volumen
        // (zwei deckungsgleiche Voll-Körper verdecken sich im 3D-Render gegenseitig).
        // Werte: 1 = nur Segmentierung (grün), 2 = nur Ground-Truth (rot), 3 = Überlappung (gelb).
        // Eigene Colormap registrieren (bei cal_min=0/cal_max=3 landen 0..3 genau auf I=0/85/170/255):
        try {
            niivueViewer.addColormap("gtcompare", {
                R: [0,   0, 255, 255],
                G: [0, 255,   0, 255],
                B: [0,   0,   0,   0],
                A: [0, 255, 255, 255],
                I: [0,  85, 170, 255]
            });
        } catch (e) { console.warn("addColormap gtcompare:", e); }

        const absoluteUrl = new URL(overlayUrl, window.location.origin).href;
        // Als einziges Volumen laden (ersetzt die reine Segmentierungs-Ansicht)
        await niivueViewer.loadVolumes([{ url: absoluteUrl }]);
        const vol = niivueViewer.volumes && niivueViewer.volumes[0];
        if (vol) {
            niivueViewer.setColormap(vol.id, "gtcompare");
            vol.cal_min = 0;
            vol.cal_max = 3;
            niivueViewer.setOpacity(0, 0.9);
        }
        niivueViewer.setSliceType(niivue.SLICE_TYPE.RENDER);
        niivueViewer.updateGLVolume();
        niivueViewer.drawScene();

        if (statusMessage) statusMessage.textContent =
            "Vergleich: grün = deine Segmentierung, rot = Ground-Truth, gelb = Überlappung.";
        console.log("[GT-Vergleich] Vergleichs-Ansicht geladen (grün=Seg, rot=GT, gelb=Überlappung).");
        // Farb-Legende NUR in der 3D-Ansicht zeigen (nicht im 2D-Overlay)
        window.gtComparisonActive = true;
        updateGtLegendVisibility();
    } catch (error) {
        console.error("Fehler beim Laden der Vergleichs-Ansicht:", error);
        alert("Die Vergleichs-Ansicht konnte nicht geladen werden.");
    } finally {
        hideBlockingLoader();
    }
}

    function populateImageList(files) {
        if (!imageListUl) return;
        imageListUl.innerHTML = '';
        if (fileType !== 'dicom_series' || !files || files.length === 0) {
            if (fileType === 'dicom_series' && imageListUl) imageListUl.innerHTML = '<li>Keine DICOM-Bilder geladen.</li>';
            return;
        }
        files.forEach((filename, index) => {
            const li = document.createElement('li'); li.className = 'image-list-item'; li.dataset.index = index;
            const span = document.createElement('span'); span.textContent = `${index + 1}: ${filename}`; span.title = `Springe zu: ${filename}`;
            li.addEventListener('click', () => { 
                // Erlaube auch im finished-Zustand das Klicken auf Slices
                if (currentIndex !== index) { 
                    loadImage(index, 'axial'); 
                } 
            });
            li.appendChild(span); imageListUl.appendChild(li);
        });
    }

    function highlightActiveListItem() {
        if (!imageListUl || fileType !== 'dicom_series') return;
        const items = imageListUl.querySelectorAll('.image-list-item'); let activeItem = null;
        items.forEach(item => {
            const itemIndex = parseInt(item.dataset.index, 10); const isActive = (itemIndex === currentIndex);
            const savedSet = savedIndicesPerAxis['axial'] || new Set(); const isSaved = savedSet.has(itemIndex);
            item.classList.remove('active-image-item', 'saved-item', 'template-applied');
            if (isSaved) item.classList.add('saved-item');
            if (isActive) { item.classList.add('active-image-item'); activeItem = item; }
        });
    }

    function updateProgressDisplay() {
        const axisForProgress = (currentWorkflowStage === 'annotate_sam' && sam2FixatedAxis) ? sam2FixatedAxis : currentAxis;
        const currentSavedSet = savedIndicesPerAxis[axisForProgress] || new Set();
        const savedCount = currentSavedSet.size;
        const totalForProgress = isVolumetric() ? (sliceCounts[axisForProgress] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
        // "X / N Slices bearbeitet"-Leiste entfernt (Nutzerwunsch). Das #progress-container-Element
        // bleibt im DOM und wird ausschließlich von showProgress()/hideProgress() für die
        // Verarbeitungsanzeige (Few-Shot/3D "Verarbeite X / Y") verwendet.
        void savedCount; void totalForProgress; void axisForProgress;
    }

    function saveCurrentSamAnnotations() {
        const axisToSave = sam2FixatedAxis || currentAxis;
        const totalForAxis = isVolumetric() ? (sliceCounts[axisToSave] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
        if (segmentationMethod === 'sam' && currentIndex >= 0 && currentIndex < totalForAxis ) {
            if (!allAnnotations[axisToSave]) allAnnotations[axisToSave] = {};
            allAnnotations[axisToSave][currentIndex] = JSON.parse(JSON.stringify(annotations));
        }
    }

    function loadSamAnnotationsForIndex(axis, index) {
        annotations = (allAnnotations[axis] && allAnnotations[axis][index]) ? JSON.parse(JSON.stringify(allAnnotations[axis][index])) : [];
        if (btnUndo) btnUndo.disabled = (annotations.length === 0);
    }

    function updateUIForStage() {
        console.log("[UI UPDATE] Stage:", currentWorkflowStage, "File:", `"${fileType}"`, "isVolumetric:",isVolumetric(), "Axis:", currentAxis, "SAM Axis:", sam2FixatedAxis);

        // Schritt-Indikator (1 Fensterung · 2 Methode · 3 Annotation · 4 Ergebnis)
        {
            const stepByStage = { wl_select: 1, method_select: 2, annotate_sam: 3, annotate_thresh: 3, finished: 4 };
            const activeStep = stepByStage[currentWorkflowStage] || 1;
            document.querySelectorAll('#workflow-stepper .wf-step').forEach(el => {
                const n = parseInt(el.dataset.step, 10);
                el.classList.toggle('active', n === activeStep);
                el.classList.toggle('done', n < activeStep);
            });
        }

        controlsWLDiv.classList.toggle('hidden', currentWorkflowStage !== 'wl_select');
        methodSelectionDiv.classList.toggle('hidden', currentWorkflowStage !== 'method_select');
        controlsAnnotateSamDiv.classList.toggle('hidden', currentWorkflowStage !== 'annotate_sam');
        controlsThresholdDiv.classList.toggle('hidden', currentWorkflowStage !== 'annotate_thresh');
        finishedMessageDiv.classList.toggle('hidden', currentWorkflowStage !== 'finished');
        if (resultActionsDiv) {
            const showResults = currentWorkflowStage === 'finished';
            resultActionsDiv.classList.toggle('hidden', !showResults);
            // Harte, klassenunabhängige Durchsetzung: Ereignis-Buttons NUR im finalen UI.
            // (inline display schlägt jede Klassenregel und kann nicht versehentlich ent-hidden werden)
            resultActionsDiv.style.display = showResults ? '' : 'none';
        }
        
        // HYBRID-ANSATZ: Verstecke Hybrid-UI bei Workflow-Wechsel
        if (currentWorkflowStage !== 'annotate_sam') {
            hideHybridRefinementButton();
            currentSamMask = null;
        }

        // Aktualisiere die fixierten WL/WW-Anzeigen
        if (fixedWL !== null && fixedWW !== null) {
            if (fixedWlDisplaySam) fixedWlDisplaySam.textContent = Math.round(fixedWL);
            if (fixedWwDisplaySam) fixedWwDisplaySam.textContent = Math.round(fixedWW);
            if (fixedWlDisplayThresh) fixedWlDisplayThresh.textContent = Math.round(fixedWL);
            if (fixedWwDisplayThresh) fixedWwDisplayThresh.textContent = Math.round(fixedWW);
        } else {
            if (fixedWlDisplaySam) fixedWlDisplaySam.textContent = '?';
            if (fixedWwDisplaySam) fixedWwDisplaySam.textContent = '?';
            if (fixedWlDisplayThresh) fixedWlDisplayThresh.textContent = '?';
            if (fixedWwDisplayThresh) fixedWwDisplayThresh.textContent = '?';
        }

        const volumetricDataLoaded = isVolumetric();
        if (imageListContainer) imageListContainer.classList.toggle('hidden', volumetricDataLoaded || currentWorkflowStage === 'finished');

        // Slider-Logik angepasst
        const showSliceSlider = (volumetricDataLoaded || fileType === 'dicom_series') && currentWorkflowStage !== 'finished';
        if (sliceSliderContainer) sliceSliderContainer.classList.toggle('hidden', !showSliceSlider);


        if (axisSelectorContainer) {
            // Achsen-Auswahl NUR in der Fensterung (wl_select). Ab „Methode wählen" ist die Achse
            // ohnehin fixiert (sam2FixatedAxis) — dort und in SAM/Threshold ausblenden.
            let showAxisSel = (currentWorkflowStage === 'wl_select' && volumetricDataLoaded);
            axisSelectorContainer.classList.toggle('hidden', !showAxisSel);
            axisButtons.forEach(btn => {
                if (btn) {
                    btn.classList.toggle('active', btn.dataset.axis === currentAxis);
                    if (currentWorkflowStage === 'annotate_sam' && sam2FixatedAxis) {
                        btn.disabled = (btn.dataset.axis !== sam2FixatedAxis);
                    } else {
                        btn.disabled = !volumetricDataLoaded;
                    }
                }
            });
        }

        if (dicomNavControlsWL) dicomNavControlsWL.classList.toggle('hidden', volumetricDataLoaded || currentWorkflowStage !== 'wl_select');
        if (dicomNavControlsSAM) {
        const hideNavSam = currentWorkflowStage !== 'annotate_sam'; // Verstecke, wenn NICHT in SAM-Annotation
        dicomNavControlsSAM.classList.toggle('hidden', hideNavSam);
        console.log("[UI BTN DEBUG] hideNavSam (dicomNavControlsSAM):", hideNavSam); // Zusätzliches Log
        }


        canvasContainer.style.display = currentWorkflowStage === 'finished' ? 'none' : 'flex';
        imageInfo.style.display = currentWorkflowStage === 'finished' ? 'none' : 'block';
        if (statusMessage) statusMessage.style.display = currentWorkflowStage === 'finished' ? 'none' : 'block';

        // View-Toggle-Container anzeigen nur in der SAM-Annotationsstufe
        if (viewToggleContainer) {
            const showToggle = (currentWorkflowStage === 'annotate_sam');
            viewToggleContainer.classList.toggle('hidden', !showToggle);
        }

        // Finaler 3D/2D-Toggle nur im Finished-Stage anzeigen
        if (finalViewToggleContainer) {
            const showFinalToggle = (currentWorkflowStage === 'finished');
            finalViewToggleContainer.classList.toggle('hidden', !showFinalToggle);
        }

        const showPreviewArea = (currentWorkflowStage === 'finished');
        if (previewAreaContainer) {
            previewAreaContainer.classList.toggle('hidden', !showPreviewArea);
            console.log('[UI] Preview Area Container sichtbar:', !previewAreaContainer.classList.contains('hidden'), 'Stage:', currentWorkflowStage);
        }
        if (showPreviewArea) {
            if (preview3dArea) preview3dArea.classList.toggle('hidden', currentWorkflowStage !== 'finished' || !niivueViewer);
        } else {
            if (preview3dArea) preview3dArea.classList.add('hidden');
            // Beim Verlassen des Finished-Stage: 2D-Container ausblenden
            if (final2dViewerContainer) final2dViewerContainer.classList.add('hidden');
        }

        // Canvas-Größe nach Layout-Änderungen neu anpassen, um "Quetschen" zu vermeiden
        try {
            if (currentWorkflowStage !== 'finished') {
                // kurze Verzögerung, bis CSS-Layout angewendet ist
                setTimeout(() => {
                    if (typeof resizeCanvasToFitContainer === 'function') {
                        resizeCanvasToFitContainer();
                    }
                    if (typeof drawImageAndAnnotations === 'function') {
                        drawImageAndAnnotations();
                    }
                }, 0);
            }
        } catch (e) { console.warn('Resize/Repaint nach UI-Update fehlgeschlagen:', e); }

        // Steuert die Sichtbarkeit der Box für Volumen- und Gewichtsinformationen
        const weightInfoDiv = document.getElementById('weight-info');
        if (weightInfoDiv) {
            const showWeightInfo = (currentWorkflowStage === 'finished');
            weightInfoDiv.classList.toggle('hidden', !showWeightInfo);
        }

        const totalSlices = isVolumetric() ? (sliceCounts[currentAxis] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
        if (btnPreviousWl) btnPreviousWl.disabled = !(fileType === 'dicom_series' && currentIndex > 0 && currentWorkflowStage === 'wl_select');
        if (btnNextWl) btnNextWl.disabled = !(fileType === 'dicom_series' && currentIndex < totalSlices - 1 && currentWorkflowStage === 'wl_select');
        if (btnPreviousSam) {
        let disablePrevSam = true; // Standardmäßig deaktiviert
        if (currentWorkflowStage === 'annotate_sam' && currentIndex > 0 && totalSlices > 0) {
        disablePrevSam = false;
        }
        btnPreviousSam.disabled = disablePrevSam;
        // console.log(`[UI BTN DEBUG] btnPreviousSam.disabled = ${disablePrevSam} (currentIndex: ${currentIndex}, stage: ${currentWorkflowStage})`);
        }

        // Button-Logik für SAM
        const axisForSAMProgress = sam2FixatedAxis || currentAxis;
        const totalForSAMAxis = isVolumetric() ? (sliceCounts[axisForSAMProgress] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
        const savedSetForSAM = savedIndicesPerAxis[axisForSAMProgress] || new Set();
        const savedCountForSAM = savedSetForSAM.size;
        const allSlicesProcessedForSAM = (totalForSAMAxis > 0 && savedCountForSAM >= totalForSAMAxis);

        console.log("[UI BTN DEBUG] Stage:", currentWorkflowStage);
        console.log("[UI BTN DEBUG] axisForSAMProgress:", axisForSAMProgress);
        console.log("[UI BTN DEBUG] totalForSAMAxis:", totalForSAMAxis);
        console.log("[UI BTN DEBUG] savedCountForSAM:", savedCountForSAM);
        console.log("[UI BTN DEBUG] allSlicesProcessedForSAM:", allSlicesProcessedForSAM);

        // Neue Button-Logik für Segment/Next
        if(btnSegmentSam) {
            let enableSegmentButton = false;
            if (currentWorkflowStage === 'annotate_sam') {
                // Segmentieren ist aktiviert wenn Annotationen vorhanden sind
                enableSegmentButton = (annotations && annotations.length > 0);
            }
            btnSegmentSam.disabled = !enableSegmentButton;
            console.log("[UI BTN DEBUG] btnSegmentSam.disabled =", !enableSegmentButton, "annotations.length =", annotations ? annotations.length : 0);
        }

        if(btnNextSam) {
            let enableNextButton = false;
            if (currentWorkflowStage === 'annotate_sam' && totalForSAMAxis > 0) {
                // Next ist immer aktiviert in der SAM-Stufe
                enableNextButton = true;
            }
            btnNextSam.disabled = !enableNextButton;
        }

        if(btnFinalizeSam) {
            let showFinalizeButton = (currentWorkflowStage === 'annotate_sam' && allSlicesProcessedForSAM && totalForSAMAxis > 0);
            console.log("[UI BTN DEBUG] showFinalizeButton (btnFinalizeSam):", showFinalizeButton);
            btnFinalizeSam.classList.toggle('hidden', !showFinalizeButton);
            btnFinalizeSam.disabled = !showFinalizeButton;
        }
        if(btnMarkOthersEmptySam){
            let showMarkEmptyButton = (currentWorkflowStage === 'annotate_sam' && totalForSAMAxis > 0 && !allSlicesProcessedForSAM );
            console.log("[UI BTN DEBUG] showMarkEmptyButton (btnMarkOthersEmptySam):", showMarkEmptyButton);
            btnMarkOthersEmptySam.classList.toggle('hidden', !showMarkEmptyButton);
            btnMarkOthersEmptySam.disabled = !showMarkEmptyButton;
        }

        updateProgressDisplay();
        setCanvasCursor();
        if (currentWorkflowStage !== 'annotate_thresh') clearPreviewCanvas();
        else if (currentRawPixelData) updateThresholdPreview();
    }

    function updateSliceSlider() {
        if (!sliceSlider || !currentSliceDisplay || !totalSlicesDisplay || !currentAxisDisplay) return;
        let currentTotal = isVolumetric() ? (sliceCounts[currentAxis] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
        if (currentTotal === 0 && totalSlicesForCurrentAxis > 0) { // Fallback, wenn sliceCounts noch nicht vom Backend aktualisiert wurden
            currentTotal = totalSlicesForCurrentAxis;
        }

        sliceSlider.max = currentTotal > 0 ? currentTotal - 1 : 0;
        sliceSlider.value = (currentIndex >= 0 && currentIndex < currentTotal) ? currentIndex : 0; // Sicherstellen, dass Wert im Bereich ist
        currentSliceDisplay.textContent = currentTotal > 0 ? Math.min(currentIndex + 1, currentTotal) : 0;
        totalSlicesDisplay.textContent = currentTotal;
        currentAxisDisplay.textContent = currentAxis.charAt(0).toUpperCase() + currentAxis.slice(1);
    }

    axisButtons.forEach(button => {
        if (button) button.addEventListener('click', async (e) => {
            const newAxis = e.target.dataset.axis;
            if (!isVolumetric() || !newAxis) return;

            if (newAxis !== currentAxis) {
                if (currentWorkflowStage === 'annotate_sam' && sam2FixatedAxis && sam2FixatedAxis !== newAxis) {
                    if (!(await showConfirmationModal("Achse wechseln?", `Wenn Sie die Achse wechseln, gehen nicht gespeicherte SAM-Annotationen für die Achse '${sam2FixatedAxis}' verloren und die Segmentierung für diese Achse wird zurückgesetzt. Fortfahren?`))) {
                    return;
                    }
                    if (savedIndicesPerAxis[sam2FixatedAxis]) savedIndicesPerAxis[sam2FixatedAxis].clear();
                    if (allAnnotations[sam2FixatedAxis]) delete allAnnotations[sam2FixatedAxis];
                    sam2FixatedAxis = null;
                    if(statusMessage) statusMessage.textContent = "SAM2-Achsenfixierung aufgehoben. Bitte neue Achse für SAM wählen oder direkt annotieren.";
                } else if (currentWorkflowStage === 'annotate_sam' && !sam2FixatedAxis) {
                     sam2FixatedAxis = newAxis;
                     if(statusMessage) statusMessage.textContent = `SAM2-Annotationen erfolgen nun für die Achse '${sam2FixatedAxis}'.`;
                }
                currentAxis = newAxis;
                currentIndex = 0;
                axisButtons.forEach(btn => {if(btn) btn.classList.toggle('active', btn.dataset.axis === newAxis)});
                loadSamAnnotationsForIndex(currentAxis, currentIndex);
                loadImage(currentIndex, currentAxis);
            } else if (newAxis === currentAxis && currentWorkflowStage === 'annotate_sam' && !sam2FixatedAxis) {
                sam2FixatedAxis = newAxis;
                if(statusMessage) statusMessage.textContent = `SAM2-Annotationen erfolgen nun für die Achse '${sam2FixatedAxis}'.`;
                updateUIForStage();
            }
        });
    });

    if (sliceSlider) sliceSlider.addEventListener('input', (e) => {
        const newIndex = parseInt(e.target.value, 10);
        const currentMax = parseInt(sliceSlider.max, 10); // Max-Wert des Sliders lesen
        if (newIndex >= 0 && newIndex <= currentMax && newIndex !== currentIndex) {
            loadImage(newIndex, currentAxis);
        } else if (newIndex > currentMax && currentMax >=0) { // Korrektur für den Fall, dass Slider über das Max hinaus geht
            sliceSlider.value = currentMax;
            // Optional: loadImage(currentMax, currentAxis); wenn das gewünscht ist
        } else if (newIndex < 0 && currentMax >=0) { // Korrektur für den Fall, dass Slider unter das Min geht
             sliceSlider.value = 0;
            // Optional: loadImage(0, currentAxis);
        }
    });

    // Verbesserte Funktion zur Anpassung der Canvas-Größe an den Container
    function resizeCanvasToFitContainer() {
        if (!canvas || !canvasContainer) return;
        
        // Originalgröße des Bildes beibehalten
        const imgWidth = originalWidth;
        const imgHeight = originalHeight;
        
        if (imgWidth <= 0 || imgHeight <= 0) return;
        
        // Canvas-Größe auf die Originalgröße des Bildes setzen
        // Dies ist die interne Zeichenfläche, die unabhängig von der angezeigten Größe ist
        canvas.width = imgWidth;
        canvas.height = imgHeight;
        
        if (previewCanvas) {
            previewCanvas.width = imgWidth;
            previewCanvas.height = imgHeight;
        }
        
        // Hole die tatsächliche Größe des Containers
        // Wir verwenden getBoundingClientRect für genauere Werte
        const containerRect = canvasContainer.getBoundingClientRect();
        const containerWidth = containerRect.width;
        const containerHeight = containerRect.height;
        
        console.log(`Container size: ${containerWidth.toFixed(0)}x${containerHeight.toFixed(0)}`);
        
        // Berechne das Seitenverhältnis des Bildes und des Containers
        const imgRatio = imgWidth / imgHeight;
        const containerRatio = containerWidth / containerHeight;
        
        let cssWidth, cssHeight;
        
        // Berechne die optimale Größe, um das Bild im Container anzuzeigen
        // und dabei das Seitenverhältnis beizubehalten
        if (imgRatio > containerRatio) {
            // Bild ist breiter als Container (relativ zur Höhe)
            cssWidth = containerWidth;
            cssHeight = containerWidth / imgRatio;
        } else {
            // Bild ist höher als Container (relativ zur Breite)
            cssHeight = containerHeight;
            cssWidth = containerHeight * imgRatio;
        }
        
        // Stelle sicher, dass die Werte gültig sind
        cssWidth = Math.max(10, Math.min(5000, cssWidth));
        cssHeight = Math.max(10, Math.min(5000, cssHeight));
        
        // Positioniere die Canvas mittig im Container
        const offsetX = (containerWidth - cssWidth) / 2;
        const offsetY = (containerHeight - cssHeight) / 2;
        
        // CSS-Größe und Position setzen
        canvas.style.width = `${cssWidth}px`;
        canvas.style.height = `${cssHeight}px`;
        canvas.style.left = `${offsetX}px`;
        canvas.style.top = `${offsetY}px`;
        
        if (previewCanvas) {
            previewCanvas.style.width = `${cssWidth}px`;
            previewCanvas.style.height = `${cssHeight}px`;
            previewCanvas.style.left = `${offsetX}px`;
            previewCanvas.style.top = `${offsetY}px`;
        }
        
        // Setze Zoom und Pan zurück, wenn die Größe geändert wird
        // Dies verhindert Probleme mit der Mausposition nach der Größenänderung
        scale = 1.0;
        originX = 0;
        originY = 0;
        
        console.log(`Canvas resized: internal ${imgWidth}x${imgHeight}, displayed ${cssWidth.toFixed(0)}x${cssHeight.toFixed(0)}, offset (${offsetX.toFixed(0)}, ${offsetY.toFixed(0)})`);
    }

    currentImage.onload = () => {
        console.log("currentImage.onload: Bild geladen, Originalgröße:", currentImage.naturalWidth, "x", currentImage.naturalHeight);
        originalWidth = currentImage.naturalWidth;
        originalHeight = currentImage.naturalHeight;
        
        if (canvas) {
            // Canvas-Größe anpassen
            resizeCanvasToFitContainer();
            scale = 1.0; originX = 0; originY = 0; debugClickPos = null;
        }
        
        showLoading(false);
        drawImageAndAnnotations();
        if (currentWorkflowStage === 'annotate_thresh' && currentRawPixelData) {
            console.log("Rufe updateThresholdPreview von currentImage.onload auf");
            updateThresholdPreview();
        }
        if (statusMessage && currentWorkflowStage !== 'finished') {
            statusMessage.textContent = "Bereit.";
        }
        updateUIForStage();
        if(fileType === 'dicom_series') highlightActiveListItem();
    };
    currentImage.onerror = (err) => {
        console.error("currentImage.onerror: Bild Ladefehler:", err);
        if (statusMessage) statusMessage.textContent = `Fehler Bilddaten (onerror).`;
        clearCanvas(); clearPreviewCanvas(); showLoading(false); updateUIForStage();
    };

    async function fetchAndUpdateImage(indexToLoad, wl = null, ww = null, axisToLoad = currentAxis) {
        console.log(`WorkspaceAndUpdateImage: index=${indexToLoad}, axis=${axisToLoad}, WL=${wl}, WW=${ww}`);
        if (indexToLoad < 0) { console.warn("fetchAndUpdateImage: Ungültiger negativer Index."); showLoading(false); return; }

        let maxSlicesForAxis = 0;
        if (fileType === 'dicom_series') {
            maxSlicesForAxis = dicomFilenames.length;
        } else if (isVolumetric()) {
            maxSlicesForAxis = sliceCounts[axisToLoad] || 0;
        }

        if (maxSlicesForAxis === 0 && indexToLoad >= 0 ) {
            console.warn(`WorkspaceAndUpdateImage: Keine Slices verfügbar für Achse ${axisToLoad} (Index ${indexToLoad} angefordert).`);
            showLoading(false);
            if(statusMessage) statusMessage.textContent = "Keine Slices für diese Achse.";
            clearCanvas(); clearPreviewCanvas(); currentRawPixelData = null;
            if(filenameSpan) filenameSpan.textContent = "Keine Slices";
            totalSlicesForCurrentAxis = 0; currentIndex = 0; // Wichtig für Slider-Update
            updateSliceSlider(); updateUIForStage();
            return;
        }
        if (indexToLoad >= maxSlicesForAxis && maxSlicesForAxis > 0) {
             console.warn(`WorkspaceAndUpdateImage: Index ${indexToLoad} außerhalb des Bereichs für Achse ${axisToLoad} (${maxSlicesForAxis}). Setze auf letzten Slice: ${maxSlicesForAxis -1}.`);
             indexToLoad = maxSlicesForAxis -1;
        }


        showLoading(true);
        if (statusMessage) statusMessage.textContent = `Lade Slice ${indexToLoad + 1} (${axisToLoad})...`;
        currentRawPixelData = null; currentRawDataType = null;
        clearPreviewCanvas();

        const queryParams = new URLSearchParams({ axis: axisToLoad });
        if (wl !== null) queryParams.append('wl', wl);
        if (ww !== null) queryParams.append('ww', ww);

        const url = `/get_image/${indexToLoad}?${queryParams.toString()}`;
        console.log("fetchAndUpdateImage: Rufe URL:", url);

        try {
            const response = await fetch(url);
            if (!response.ok) {
                const errorText = await response.text();
                console.error("fetchAndUpdateImage: HTTP Fehler Response Text:", errorText);
                let errorData = { error: `HTTP Fehler ${response.status}` };
                try { errorData = JSON.parse(errorText); } catch (e) { /* Behalte default errorData */ }
                throw new Error(errorData.error || `HTTP Fehler ${response.status}`);
            }
            const data = await response.json();
            console.log("fetchAndUpdateImage: Antwort vom Backend:", data);
            if (data.error) { throw new Error(data.error); }

            currentDisplayedFilename = data.filename;
            if (filenameSpan) filenameSpan.textContent = currentDisplayedFilename;

            if (data.slice_counts) sliceCounts = data.slice_counts; // Slice Counts vom Backend übernehmen
            if (data.current_axis) currentAxis = data.current_axis;
            if (data.original_filename) baseVolumeFilename = data.original_filename;


            if (data.used_wl !== undefined && data.used_ww !== undefined) {
                currentWL = data.used_wl; currentWW = data.used_ww;
                if (currentWorkflowStage === 'wl_select') updateWLWWControls();
            }
            // Dynamische Slider-Ranges an Datenbereich anpassen
            if (data.data_min !== undefined && data.data_max !== undefined) {
                const dMin = Math.floor(data.data_min);
                const dMax = Math.ceil(data.data_max);
                if (sliderWL) { sliderWL.min = dMin; sliderWL.max = dMax; }
                if (sliderWW) { sliderWW.min = 1; sliderWW.max = Math.max(100, dMax - dMin); }
            }
            // Globalen HU-Bereich des Volumens merken (für die Verfeinerungs-Regler)
            if (data.global_data_min !== undefined && data.global_data_min !== null &&
                data.global_data_max !== undefined && data.global_data_max !== null) {
                globalDataMin = data.global_data_min;
                globalDataMax = data.global_data_max;
            }
            if (data.raw_pixel_data_b64 && data.raw_data_dtype && data.raw_data_shape) {
                const rawBytes = Uint8Array.from(atob(data.raw_pixel_data_b64), c => c.charCodeAt(0));
                currentRawDataType = data.raw_data_dtype;
                const shape = data.raw_data_shape;
                const numPixels = shape[0] * shape[1];
                let TypedArrayConstructor;
                if (currentRawDataType.startsWith('float64')) TypedArrayConstructor = Float64Array;
                else if (currentRawDataType.startsWith('float32')) TypedArrayConstructor = Float32Array;
                else if (currentRawDataType.startsWith('int32')) TypedArrayConstructor = Int32Array;
                else if (currentRawDataType.startsWith('uint32')) TypedArrayConstructor = Uint32Array;
                else if (currentRawDataType.startsWith('int16')) TypedArrayConstructor = Int16Array;
                else if (currentRawDataType.startsWith('uint16')) TypedArrayConstructor = Uint16Array;
                else if (currentRawDataType.startsWith('int8')) TypedArrayConstructor = Int8Array;
                else if (currentRawDataType.startsWith('uint8')) TypedArrayConstructor = Uint8Array;
                else { console.warn(`Unbekannter Rohdaten-Typ: ${currentRawDataType}`); TypedArrayConstructor = Float32Array; }

                if (rawBytes.byteLength !== numPixels * TypedArrayConstructor.BYTES_PER_ELEMENT) {
                    console.error(`Byte-Länge (${rawBytes.byteLength}) passt nicht zu Shape (${shape}) und Typ (${currentRawDataType}). Erwartet: ${numPixels * TypedArrayConstructor.BYTES_PER_ELEMENT}`);
                    currentRawPixelData = null;
                } else {
                    currentRawPixelData = new TypedArrayConstructor(rawBytes.buffer);
                    console.log("fetchAndUpdateImage: Rohdaten erfolgreich dekodiert. Länge:", currentRawPixelData.length, "Erwartet:", numPixels);
                }
                if (currentWorkflowStage === 'annotate_thresh' && currentRawPixelData) updateThresholdSliderMinMax(currentRawPixelData);

            } else {
                console.warn("fetchAndUpdateImage: Keine Rohdaten vom Backend empfangen oder Parsing-Fehler.");
                currentRawPixelData = null;
                if (btnSelectThresh) btnSelectThresh.disabled = true;
            }

            if (data.image_data) {
                console.log("fetchAndUpdateImage: Setze currentImage.src");
                currentImage.src = data.image_data;
                if(statusMessage) statusMessage.textContent = `Slice ${indexToLoad + 1} (${axisToLoad}) geladen.`;
            } else {
                throw new Error("Keine 8-bit Bilddaten (data.image_data) empfangen.");
            }

        } catch (error) {
            console.error("Fehler in fetchAndUpdateImage:", error);
            if (statusMessage) statusMessage.textContent = `Fehler beim Laden des Bildes: ${error.message}`;
            showLoading(false); clearCanvas(); clearPreviewCanvas();
        } finally {
            showLoading(false);
            updateSliceSlider(); // Wichtig, falls sich sliceCounts durch Backend-Antwort geändert haben
            updateUIForStage();
            if(fileType === 'dicom_series') highlightActiveListItem();
        }
    }

    async function loadImage(index, axis = currentAxis) {
        console.log(`loadImage: Fordere Slice ${index} für Achse ${axis} an.`);
        const previousIndex = currentIndex;
        const previousAxis = currentAxis;

        let targetTotalSlices = isVolumetric() ? (sliceCounts[axis] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
        console.log(`loadImage: targetTotalSlices für Achse ${axis} ist ${targetTotalSlices}`);

        if (targetTotalSlices === 0 && index === 0) {
             console.log("loadImage: targetTotalSlices ist 0, versuche trotzdem initial zu laden.");
        } else if (index >= targetTotalSlices && targetTotalSlices > 0) {
            console.log(`loadImage: Index ${index} ist am Ende oder darüber. Vorheriger Index: ${previousIndex}, targetTotalSlices: ${targetTotalSlices}`);
            const canFinalize = (currentWorkflowStage === 'annotate_thresh') ||
                                 (currentWorkflowStage === 'annotate_sam' && (!isVolumetric() || sam2FixatedAxis === previousAxis));
            if ((currentWorkflowStage === 'annotate_sam' || currentWorkflowStage === 'annotate_thresh') && previousIndex === targetTotalSlices - 1 && canFinalize) {
                console.log("loadImage: Am letzten Slice einer Annotationsstufe, starte Finalisierung.");
                if (currentWorkflowStage === 'annotate_sam') saveCurrentSamAnnotations();
                await triggerFinishSequence(segmentationMethod, sam2FixatedAxis || previousAxis);
            } else {
                console.log("loadImage: Nicht letzter Slice einer Annotationsstufe oder Finalisierung nicht möglich, gehe zu 'finished'.");
                if (currentWorkflowStage !== 'finished') {
                    currentIndex = targetTotalSlices > 0 ? targetTotalSlices : 0;
                    currentWorkflowStage = 'finished';
                }
                updateUIForStage(); showLoading(false);
            }
            return;
        }
        if (index < 0) {
            console.log("loadImage: Index < 0, setze auf 0.");
            currentIndex = 0;
            if (sliceSlider) sliceSlider.value = currentIndex;
            updateUIForStage(); showLoading(false);
            return;
        }

        if (previousIndex >= 0 && currentWorkflowStage === 'annotate_sam' && (!isVolumetric() || previousAxis === sam2FixatedAxis) ) {
            console.log(`loadImage: Speichere SAM Annotationen für vorherigen Slice ${previousIndex} der Achse ${previousAxis}`);
            saveCurrentSamAnnotations();
        }

        currentIndex = index;
        currentAxis = axis;
        console.log(`loadImage: Setze currentIndex=${currentIndex}, currentAxis=${currentAxis}`);

        // HYBRID-ANSATZ: Verstecke Hybrid-UI beim Slice-Wechsel
        hideHybridRefinementButton();
        currentSamMask = null;

        loadSamAnnotationsForIndex(currentAxis, currentIndex);
        resetZoomAndPan();

        // Prüfe ob eine Segmentierung für aktuellen Slice existiert
        const savedSet = savedIndicesPerAxis[currentAxis] || new Set();
        const savedMethod = savedMethodPerSlice[`${currentAxis}_${currentIndex}`];
        const isSamSegmented = savedMethod === 'sam' || savedMethod === 'sam_template_applied';
        
        if ((currentWorkflowStage === 'annotate_sam' || currentWorkflowStage === 'finished') && savedSet.has(currentIndex) && isSamSegmented) {
            let overlayFilename;
            if (isVolumetric()) {
                // Für Volumen-Daten: baseVolumeFilename_achse_index_overlay.png
                overlayFilename = `${baseVolumeFilename}_${currentAxis}_${String(currentIndex).padStart(3, '0')}_overlay.png`;
            } else {
                // Für DICOM-Serien: original_filename_overlay.png
                const baseNameForOverlay = dicomFilenames[currentIndex] ? dicomFilenames[currentIndex].replace(/\.(dcm|dicom)$/i, '') : 'unknown_slice';
                overlayFilename = `${baseNameForOverlay}_overlay.png`;
            }
            currentSegmentationUrl = `/overlays_archive/${overlayFilename}`;
            
            // Toggle-Button aktivieren
            if (btnViewSegmentation) {
                btnViewSegmentation.disabled = false;
                btnViewSegmentation.classList.remove('disabled');
            }
        } else {
            currentSegmentationUrl = null;
            
            // Toggle-Button deaktivieren
            if (btnViewSegmentation) {
                btnViewSegmentation.disabled = true;
                btnViewSegmentation.classList.add('disabled');
                // Zurück zur Original-Ansicht wenn keine Segmentierung vorhanden
                if (isShowingSegmentation) {
                    isShowingSegmentation = false;
                    if (btnViewOriginal) btnViewOriginal.classList.add('active');
                    if (btnViewSegmentation) btnViewSegmentation.classList.remove('active');
                }
            }
        }

        let wlToUse = (currentWorkflowStage === 'wl_select') ? currentWL : fixedWL;
        let wwToUse = (currentWorkflowStage === 'wl_select') ? currentWW : fixedWW;

        await fetchAndUpdateImage(currentIndex, wlToUse, wwToUse, currentAxis);
    }

    function updateWLWWControls() {
        if (currentWL === null || currentWW === null) return;
        settingSlidersProgrammatically = true;
        try {
            if (sliderWL && inputWL) {
                sliderWL.value = currentWL;
                inputWL.value = Math.round(currentWL);
            }
            if (sliderWW && inputWW) {
                sliderWW.value = currentWW;
                inputWW.value = Math.round(Math.max(1, currentWW));
            }
        } catch (e) {
            console.error("Error updating WL/WW controls", e);
        } finally {
            setTimeout(() => { settingSlidersProgrammatically = false; }, 50);
        }
    }

    // Event-Listener für Navigation-Buttons in der Fensterungsstufe
    if (btnPreviousWl) {
        btnPreviousWl.addEventListener('click', () => {
            console.log("[BTN PREVIOUS_WL CLICK] '< Vorheriges' (WL) geklickt.");
            if (currentWorkflowStage !== 'wl_select') {
                console.warn("[BTN PREVIOUS_WL CLICK] Falscher Workflow-Stage:", currentWorkflowStage);
                return;
            }

            if (currentIndex > 0) {
                console.log(`[BTN PREVIOUS_WL CLICK] Lade vorherigen Slice: ${currentIndex - 1}`);
                loadImage(currentIndex - 1, currentAxis);
            } else {
                console.log("[BTN PREVIOUS_WL CLICK] Bereits auf dem ersten Slice.");
            }
        });
    }

    if (btnNextWl) {
        btnNextWl.addEventListener('click', () => {
            console.log("[BTN NEXT_WL CLICK] 'Nächstes >' (WL) geklickt.");
            if (currentWorkflowStage !== 'wl_select') {
                console.warn("[BTN NEXT_WL CLICK] Falscher Workflow-Stage:", currentWorkflowStage);
                return;
            }

            const totalSlices = isVolumetric() ? (sliceCounts[currentAxis] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
            
            if (currentIndex < totalSlices - 1) {
                console.log(`[BTN NEXT_WL CLICK] Lade nächsten Slice: ${currentIndex + 1}`);
                loadImage(currentIndex + 1, currentAxis);
            } else {
                console.log("[BTN NEXT_WL CLICK] Bereits auf dem letzten Slice.");
            }
        });
    }

    if (btnPreviousSam) {
        btnPreviousSam.addEventListener('click', () => {
            console.log("[BTN PREVIOUS_SAM CLICK] '< Vorheriges' (SAM) geklickt.");
            if (currentWorkflowStage !== 'annotate_sam') {
                console.warn("[BTN PREVIOUS_SAM CLICK] Falscher Workflow-Stage:", currentWorkflowStage);
                return;
            }

            const navAxis = isVolumetric() ? (sam2FixatedAxis || currentAxis) : 'axial';
            // Die Logik für totalSlicesForNav wird hier nicht direkt für die "disabled" Prüfung benötigt,
            // da das schon in updateUIForStage passiert, aber schadet nicht für Konsistenz.
            // const totalForNav = isVolumetric() ? (sliceCounts[navAxis] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);

            if (currentIndex > 0) {
                console.log(`[BTN PREVIOUS_SAM CLICK] Lade vorherigen Slice: ${currentIndex - 1} auf Achse ${navAxis}`);
                // Wenn Annotationen auf dem aktuellen Slice gemacht wurden, könnten diese hier
                // optional gespeichert werden, bevor zum vorherigen Slice gewechselt wird.
                // Fürs Erste: Direkter Wechsel.
                // if (annotations.length > 0 && confirm("Nicht gespeicherte Annotationen für den aktuellen Slice vorhanden. Trotzdem wechseln?")) {
                //     loadImage(currentIndex - 1, navAxis);
                // } else if (annotations.length === 0) {
                //     loadImage(currentIndex - 1, navAxis);
                // }
                loadImage(currentIndex - 1, navAxis);
            } else {
                console.log("[BTN PREVIOUS_SAM CLICK] Bereits auf dem ersten Slice.");
                // Optional: Visuelles Feedback, dass man schon am Anfang ist.
            }
        });
    }

        // Neuer Event-Listener für Segment-Button
    if (btnSegmentSam) {
        btnSegmentSam.addEventListener('click', async () => { // async, da wir auf Backend warten
        console.log("[BTN SAVESAM CLICK] 'Segmentieren (SAM) & Weiter' geklickt.");
        if (currentWorkflowStage !== 'annotate_sam') {
            console.warn("[BTN SAVESAM CLICK] Falscher Workflow-Stage:", currentWorkflowStage);
            return;
        }
        if (!annotations || annotations.length === 0) {
            if (!(await showConfirmationModal("Leerer Slice?", "Keine Annotationen vorhanden. Wollen Sie diesen Slice als 'leer' speichern und fortfahren? (Dies zählt als bearbeitet)"))) {
                console.log("[BTN SAVESAM CLICK] Benutzer hat 'leer speichern' abgebrochen.");
                return;
            }
            // Benutzer will als leer speichern
             const axisToSave = sam2FixatedAxis || currentAxis;
             const totalForAxis = isVolumetric() ? (sliceCounts[axisToSave] || 0) : dicomFilenames.length;
             if (currentIndex >= 0 && currentIndex < totalForAxis) {
                if (!savedIndicesPerAxis[axisToSave]) savedIndicesPerAxis[axisToSave] = new Set();
                savedIndicesPerAxis[axisToSave].add(currentIndex);
                savedMethodPerSlice[`${axisToSave}_${currentIndex}`] = 'sam_empty'; // Markieren als leer gespeichert
                if (allAnnotations[axisToSave] && allAnnotations[axisToSave][currentIndex]) {
                    delete allAnnotations[axisToSave][currentIndex]; // Alte Annotationen löschen
                }
                console.log(`[BTN SAVESAM CLICK] Slice ${currentIndex} der Achse ${axisToSave} als 'sam_empty' markiert.`);
                updateProgressDisplay();
                if(fileType === 'dicom_series') highlightActiveListItem();
             }
        } else {
            // Annotationen vorhanden, normal segmentieren
            if(statusMessage) statusMessage.textContent = "Segmentiere aktuellen Slice (SAM)...";
            showLoading(true);

            // ANPASSUNG HIER: Suche nach 'box' ODER 'freehand' Annotation
            const boxAnnotation = annotations.find(a => a.type === 'box' || a.type === 'freehand');
            const payload = {
                box: boxAnnotation ? boxAnnotation.data : null, // Verwende 'data' für die Bounding Box
                points: annotations.filter(a => a.type === 'positive' || a.type === 'negative').map(p => ({ coords: p.data, label: p.type === 'positive' ? 1 : 0 })),
                wl: fixedWL,
                ww: fixedWW,
                axis: currentAxis,
                sliceIndex: currentIndex,
                original_filename: baseVolumeFilename, // Für Volumen
                dicom_slice_filename: (fileType === 'dicom_series' && dicomFilenames[currentIndex]) ? dicomFilenames[currentIndex] : null
            };
            console.log("[BTN SAVESAM CLICK] Sende Payload an /segment_sam:", payload);

            try {
                const response = await fetch('/segment_sam', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const result = await response.json();
                showLoading(false);

                if (!response.ok || result.error) {
                    throw new Error(result.error || `Serverfehler ${response.status}`);
                }

                if (result.mask_saved) { // Auch wenn keine Annotationen, aber als leer markiert wurde nicht hier landen
                    if(statusMessage) statusMessage.textContent = `SAM für Slice ${currentIndex + 1} (${currentAxis}) gespeichert. Segmentierung verfügbar.`;
                    const axisToSave = sam2FixatedAxis || currentAxis;
                    if (!savedIndicesPerAxis[axisToSave]) savedIndicesPerAxis[axisToSave] = new Set();
                    savedIndicesPerAxis[axisToSave].add(currentIndex);
                    savedMethodPerSlice[`${axisToSave}_${currentIndex}`] = 'sam'; // Markieren als mit SAM bearbeitet
                    saveCurrentSamAnnotations(); // Speichert die aktuellen `annotations` in `allAnnotations`
                    
                    // Speichere Segmentierung URL und aktiviere Toggle-Button
                    currentSegmentationUrl = result.overlay_url;
                    if (btnViewSegmentation) {
                        btnViewSegmentation.disabled = false;
                        btnViewSegmentation.classList.remove('disabled');
                    }
                    
                    // Automatisch zur Segmentierungsansicht wechseln
                    switchToView('segmentation');
                    
                    updateProgressDisplay();
                    if(fileType === 'dicom_series') highlightActiveListItem();

                    // Few-Shot-Liste nach Segmentierung aktualisieren
                    updateFewshotMaskList();
                    
                    // HYBRID-ANSATZ: Zeige Verfeinerungs-Button nach erfolgreicher SAM-Segmentierung
                    if (result.mask_data && result.mask_data.length > 0) {
                        console.log("[HYBRID] SAM-Masken-Daten empfangen, Länge:", result.mask_data.length);
                        // Reshape the flat array back to 2D dimensions
                        currentSamMask = new Uint8Array(result.mask_data);
                        console.log("[HYBRID] currentSamMask gesetzt für Slice", currentIndex, "Achse", currentAxis);
                        showHybridRefinementButton();
                        console.log("[HYBRID] Verfeinerungs-Button angezeigt für aktuellen Slice");
                    } else {
                        console.warn("[HYBRID] Keine Masken-Daten vom Backend empfangen");
                    }

                } else if (result.message === "Keine Annotationen.") {
                     // Sollte durch die confirm-Abfrage oben abgedeckt sein, aber als Fallback
                    if(statusMessage) statusMessage.textContent = `Keine Annotationen für Slice ${currentIndex + 1} (${currentAxis}).`;
                } else {
                     if(statusMessage) statusMessage.textContent = `Maske nicht gespeichert: ${result.message || 'Unbekannter Grund'}`;
                }

            } catch (error) {
                showLoading(false);
                console.error("[BTN SAVESAM CLICK] Fehler bei SAM-Segmentierung:", error);
                if(statusMessage) statusMessage.textContent = `Fehler SAM: ${error.message}`;
                // Hier nicht automatisch zum nächsten Bild gehen, Nutzer soll Fehler sehen
                return; // Wichtig: Bei Fehler hier abbrechen, nicht zum nächsten Slice
            }
        } // Ende else (Annotationen vorhanden)
    });
}

    async function reloadImageWithCurrentWLWW() {
        if (currentWorkflowStage !== 'wl_select') return;
        // currentIndex und currentAxis sollten bereits korrekt sein
        if(statusMessage) statusMessage.textContent = `Aktualisiere Fensterung (WL: ${Math.round(currentWL)}, WW: ${Math.round(currentWW)})...`;
        await fetchAndUpdateImage(currentIndex, currentWL, currentWW, currentAxis);
    }
    const debouncedReload = debounce(reloadImageWithCurrentWLWW, 350);

    if (sliderWL) sliderWL.addEventListener('input', (event) => {
        if (currentWorkflowStage !== 'wl_select' || settingSlidersProgrammatically) return;
        currentWL = parseFloat(event.target.value);
        if (inputWL) inputWL.value = Math.round(currentWL);
        debouncedReload();
    });
    if (sliderWW) sliderWW.addEventListener('input', (event) => {
        if (currentWorkflowStage !== 'wl_select' || settingSlidersProgrammatically) return;
        currentWW = parseFloat(event.target.value);
        if (inputWW) inputWW.value = Math.round(Math.max(1, currentWW));
        debouncedReload();
    });
    if (inputWL) inputWL.addEventListener('change', (event) => {
        if (currentWorkflowStage !== 'wl_select') return;
        let val = parseFloat(event.target.value);
        if (!isNaN(val)) {
            currentWL = val;
            updateWLWWControls();
            reloadImageWithCurrentWLWW();
        } else { // Wert zurücksetzen, wenn ungültig
            if (inputWL) inputWL.value = Math.round(currentWL);
        }
    });
    if (inputWW) inputWW.addEventListener('change', (event) => {
        if (currentWorkflowStage !== 'wl_select') return;
        let val = parseFloat(event.target.value);
        if (!isNaN(val)) {
            currentWW = Math.max(1, val); // WW muss > 0 sein
            updateWLWWControls();
            reloadImageWithCurrentWLWW();
        } else { // Wert zurücksetzen, wenn ungültig
            if (inputWW) inputWW.value = Math.round(Math.max(1, currentWW));
        }
    });
    if (btnResetWLWW) btnResetWLWW.addEventListener('click', async () => {
        if (currentWorkflowStage !== 'wl_select') return;
        if (await showConfirmationModal("Fensterung zurücksetzen?", "Fensterung auf die Anfangswerte (wie beim ersten Öffnen) zurücksetzen?")) {
            // currentWL/WW auf null setzen -> loadImage sendet keine WL/WW-Parameter mehr, das Backend
            // wendet die Start-Fensterung (DEFAULT_WL/WW bzw. DICOM-Defaults) an. Die Antwort füllt
            // currentWL/WW über used_wl/used_ww neu. So exakt wie beim ersten Laden.
            currentWL = null;
            currentWW = null;
            loadImage(currentIndex, currentAxis);
        }
    });
    if (presetButtons) presetButtons.forEach(button => {
        button.addEventListener('click', (event) => {
            if (currentWorkflowStage !== 'wl_select') return;
            const presetWl = parseFloat(event.target.dataset.wl);
            const presetWw = parseFloat(event.target.dataset.ww);
            if (!isNaN(presetWl) && !isNaN(presetWw) && presetWw >= 1) {
                currentWL = presetWl;
                currentWW = presetWw;
                updateWLWWControls();
                reloadImageWithCurrentWLWW();
                // aktives Preset hervorheben (Geschwister zurücksetzen)
                presetButtons.forEach(b => b.classList.toggle('active', b === button));
            }
        });
    });
    // Bei manueller WL/WW-Änderung die Preset-Hervorhebung wieder aufheben
    ['slider-wl', 'slider-ww', 'input-wl', 'input-ww'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', () => {
            if (presetButtons) presetButtons.forEach(b => b.classList.remove('active'));
        });
    });
    if (dicomControlsDiv) dicomControlsDiv.addEventListener('click', (event) => {
        const button = event.target.closest('.step-btn');
        if (!button || currentWorkflowStage !== 'wl_select') return;

        const inputId = button.dataset.targetInput;
        const sliderId = button.dataset.targetSlider;
        const step = parseFloat(button.dataset.step) || 0;
        const inputElement = document.getElementById(inputId);
        const sliderElement = document.getElementById(sliderId);

        if (!inputElement || !sliderElement || step === 0) return;

        let currentValue = parseFloat(inputElement.value);
        if (isNaN(currentValue)) { // Fallback auf Slider, falls Input leer/ungültig
             currentValue = parseFloat(sliderElement.value) || (inputId === 'input-wl' ? 0 : 1);
        }
        let newValue = currentValue + step;
        const min = parseFloat(sliderElement.min);
        const max = parseFloat(sliderElement.max);
        newValue = Math.max(min, Math.min(max, newValue));

        if (inputId === 'input-wl') currentWL = newValue;
        else if (inputId === 'input-ww') currentWW = Math.max(1, newValue); // WW muss > 0

        updateWLWWControls();
        debouncedReload();
    });

    function updateThresholdDisplays() {
        if (displayThreshLower) displayThreshLower.textContent = Math.round(currentLowerThresh);
        if (displayThreshUpper) displayThreshUpper.textContent = Math.round(currentUpperThresh);
    }

    function updateThresholdSliderMinMax(rawDataArray) {
        if (!rawDataArray || rawDataArray.length === 0 || !sliderThreshLower || !sliderThreshUpper || !inputThreshLower || !inputThreshUpper) {
            const defaultMin = -1000, defaultMax = 1000;
            if(sliderThreshLower){ sliderThreshLower.min = defaultMin; sliderThreshLower.max = defaultMax; }
            if(inputThreshLower){ inputThreshLower.min = defaultMin; inputThreshLower.max = defaultMax; }
            if(sliderThreshUpper){ sliderThreshUpper.min = defaultMin; sliderThreshUpper.max = defaultMax; }
            if(inputThreshUpper){ inputThreshUpper.min = defaultMin; inputThreshUpper.max = defaultMax; }
            // Setze aktuelle Werte auf Mitte des Default-Bereichs oder behalte alte, wenn sie im Bereich sind
            currentLowerThresh = Math.max(defaultMin, Math.min(defaultMax, currentLowerThresh));
            currentUpperThresh = Math.max(defaultMin, Math.min(defaultMax, currentUpperThresh));
            if (currentLowerThresh > currentUpperThresh) currentLowerThresh = currentUpperThresh; // Sicherstellen

            if(sliderThreshLower) sliderThreshLower.value = currentLowerThresh;
            if(inputThreshLower) inputThreshLower.value = Math.round(currentLowerThresh);
            if(sliderThreshUpper) sliderThreshUpper.value = currentUpperThresh;
            if(inputThreshUpper) inputThreshUpper.value = Math.round(currentUpperThresh);
            updateThresholdDisplays();
            return;
        }

        let minVal = Infinity, maxVal = -Infinity;
        for (let i = 0; i < rawDataArray.length; i++) {
            if (rawDataArray[i] < minVal) minVal = rawDataArray[i];
            if (rawDataArray[i] > maxVal) maxVal = rawDataArray[i];
        }
        if (minVal === Infinity || maxVal === -Infinity || minVal >= maxVal) { // Fallback wenn keine gültigen Daten
            minVal = -1000; maxVal = 1000;
            console.warn("Threshold Min/Max Fallback auf Standardwerte, da keine gültigen Daten oder min >= max.");
        } else {
            minVal = Math.floor(minVal); // Aufrunden/Abrunden für schönere Slider-Grenzen
            maxVal = Math.ceil(maxVal);
        }

        sliderThreshLower.min = minVal; sliderThreshLower.max = maxVal; inputThreshLower.min = minVal; inputThreshLower.max = maxVal;
        sliderThreshUpper.min = minVal; sliderThreshUpper.max = maxVal; inputThreshUpper.min = minVal; inputThreshUpper.max = maxVal;

        // Stelle sicher, dass aktuelle Werte innerhalb der neuen Grenzen liegen
        currentLowerThresh = Math.max(minVal, Math.min(maxVal, currentLowerThresh));
        currentUpperThresh = Math.max(minVal, Math.min(maxVal, currentUpperThresh));
        if (currentLowerThresh > currentUpperThresh) currentLowerThresh = currentUpperThresh; // Untere darf nicht größer als obere sein

        sliderThreshLower.value = currentLowerThresh; inputThreshLower.value = Math.round(currentLowerThresh);
        sliderThreshUpper.value = currentUpperThresh; inputThreshUpper.value = Math.round(currentUpperThresh);
        updateThresholdDisplays();
    }

    function updateThresholdPreview() {
        if (currentWorkflowStage !== 'annotate_thresh' || !currentRawPixelData || !previewCtx || !previewCanvas) {
            clearPreviewCanvas(); return;
        }
        const lower = currentLowerThresh; const upper = currentUpperThresh;
        const width = originalWidth; const height = originalHeight;

        if (!thresholdMaskData || thresholdMaskData.width !== width || thresholdMaskData.height !== height) {
            try { thresholdMaskData = previewCtx.createImageData(width, height); }
            catch(e) { console.error("Create ImageData Error:", e); return; }
        }
        const data = thresholdMaskData.data; let hasMaskedPixels = false;
        for (let y = 0; y < height; y++) { for (let x = 0; x < width; x++) {
            const i1D = y * width + x; const i4D = i1D * 4; const pVal = currentRawPixelData[i1D];
            if (pVal >= lower && pVal <= upper) { data[i4D]=0; data[i4D+1]=255; data[i4D+2]=0; data[i4D+3]=150; hasMaskedPixels=true; } // Grün, halbtransparent
            else { data[i4D+3]=0; } // Komplett transparent für nicht-maskierte Bereiche
        }}

        if (!offscreenMaskCanvas || offscreenMaskCanvas.width !== width || offscreenMaskCanvas.height !== height) {
            try {
                if(typeof OffscreenCanvas !== 'undefined') offscreenMaskCanvas = new OffscreenCanvas(width,height);
                else {offscreenMaskCanvas = document.createElement('canvas'); offscreenMaskCanvas.width=width; offscreenMaskCanvas.height=height;}
                offscreenMaskCtx = offscreenMaskCanvas.getContext('2d');
            } catch(e){ console.error("Offscreen Canvas Error:", e); return; }
        }
        if(hasMaskedPixels) offscreenMaskCtx.putImageData(thresholdMaskData,0,0); else offscreenMaskCtx.clearRect(0,0,width,height);

        previewCtx.save(); previewCtx.setTransform(1,0,0,1,0,0); previewCtx.clearRect(0,0,previewCanvas.width,previewCanvas.height);
        previewCtx.translate(originX, originY); previewCtx.scale(scale, scale);
        if(offscreenMaskCanvas && hasMaskedPixels) { try { previewCtx.imageSmoothingEnabled=false; previewCtx.drawImage(offscreenMaskCanvas,0,0); } catch(e){console.error("Draw offscreen error", e)} }
        previewCtx.restore();
    }
    const debouncedUpdateThresholdPreview = debounce(updateThresholdPreview, 150);

    function handleThresholdChange() {
        if (currentLowerThresh > currentUpperThresh) {
            if (this === sliderThreshLower || this === inputThreshLower) {
                currentUpperThresh = currentLowerThresh;
                if(sliderThreshUpper) sliderThreshUpper.value = currentUpperThresh;
                if(inputThreshUpper) inputThreshUpper.value = Math.round(currentUpperThresh);
            } else {
                currentLowerThresh = currentUpperThresh;
                if(sliderThreshLower) sliderThreshLower.value = currentLowerThresh;
                if(inputThreshLower) inputThreshLower.value = Math.round(currentLowerThresh);
            }
        }
        updateThresholdDisplays();
        debouncedUpdateThresholdPreview();
    }

    if(sliderThreshLower) sliderThreshLower.addEventListener('input', function(){currentLowerThresh=parseFloat(this.value); if(inputThreshLower)inputThreshLower.value=Math.round(currentLowerThresh); handleThresholdChange.call(this);});
    if(sliderThreshUpper) sliderThreshUpper.addEventListener('input', function(){currentUpperThresh=parseFloat(this.value); if(inputThreshUpper)inputThreshUpper.value=Math.round(currentUpperThresh); handleThresholdChange.call(this);});
    if(inputThreshLower) inputThreshLower.addEventListener('change', function(){let v=parseFloat(this.value); if(!isNaN(v)){currentLowerThresh=v; if(sliderThreshLower)sliderThreshLower.value=currentLowerThresh; handleThresholdChange.call(this);}else{this.value=Math.round(currentLowerThresh);}});
    if(inputThreshUpper) inputThreshUpper.addEventListener('change', function(){let v=parseFloat(this.value); if(!isNaN(v)){currentUpperThresh=v; if(sliderThreshUpper)sliderThreshUpper.value=currentUpperThresh; handleThresholdChange.call(this);}else{this.value=Math.round(currentUpperThresh);}});
    if(btnResetThresh) btnResetThresh.addEventListener('click', () => {
        if(currentWorkflowStage !== 'annotate_thresh') return;
        // Setze auf Min/Max des Sliders oder Defaultwerte
        currentLowerThresh = parseFloat(sliderThreshLower?.min || -1000);
        currentUpperThresh = parseFloat(sliderThreshUpper?.max || 1000);
        if(sliderThreshLower)sliderThreshLower.value=currentLowerThresh; if(inputThreshLower)inputThreshLower.value=Math.round(currentLowerThresh);
        if(sliderThreshUpper)sliderThreshUpper.value=currentUpperThresh; if(inputThreshUpper)inputThreshUpper.value=Math.round(currentUpperThresh);
        updateThresholdDisplays(); updateThresholdPreview(); if(statusMessage)statusMessage.textContent="Thresholds zurückgesetzt.";
    });
    if(thresholdControlsDiv) thresholdControlsDiv.addEventListener('click', (e) => {
        const button = e.target.closest('.step-btn'); if (!button || currentWorkflowStage !== 'annotate_thresh') return;
        const inputId = button.dataset.targetInput; const sliderId = button.dataset.targetSlider; const step = parseFloat(button.dataset.step) || 0;
        const inputEl = document.getElementById(inputId); const sliderEl = document.getElementById(sliderId);
        if(!inputEl || !sliderEl || step === 0) return;
        let currentVal = inputId === 'input-thresh-lower' ? currentLowerThresh : currentUpperThresh;
        let newVal = currentVal + step;
        const min = parseFloat(sliderEl.min); const max = parseFloat(sliderEl.max); newVal = Math.max(min, Math.min(max, newVal));
        if(inputId === 'input-thresh-lower') currentLowerThresh = newVal; else currentUpperThresh = newVal;
        inputEl.value = Math.round(newVal); sliderEl.value = newVal;
        handleThresholdChange.call(inputEl);
    });

    function setTool(toolName) { if (currentWorkflowStage !== 'annotate_sam') return; if (currentTool === 'box' && isDrawing) { isDrawing = false; startX = undefined; drawImageAndAnnotations(); } currentTool = toolName; toolButtonsSAM.forEach(btn => { if(btn) btn.classList.toggle('active', btn.dataset.tool === toolName); }); setCanvasCursor(); if (statusMessage) statusMessage.textContent = toolName ? `Modus (SAM): ${toolName}` : "Annotation (SAM) bereit."; }
    function setCanvasCursor() { 
        if (!canvas) return; 
        if (isPanning) { 
            canvas.style.cursor = 'grabbing'; 
            return; 
        } 
        if (currentWorkflowStage === 'annotate_sam') { 
            switch (currentTool) { 
                case 'box': 
                    canvas.style.cursor = 'crosshair'; 
                    return; 
                case 'freehand':
                    // SVG-Stift-Icon als Cursor (schwarz, 24x24, Spitze unten links)
                    canvas.style.cursor = 'url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0Ij48cGF0aCBmaWxsPSIjZmZmZmZmIiBkPSJNMCAyMC43NXYzLjI1aDMuMjVMMTcuODEgOS41N2wtMy4yNS0zLjI1TDBiMTAuMjV6bTI0LTExLjUybC0yLjcxLTIuNzFhMS4wMSAxLjAxIDAgMDAtMS40MiAwbC0xLjg0IDEuODQgMy4xMyAzLjEzIDEuODQtMS44NGExLjAxIDEuMDEgMCAwIDAgMC0xLjQyek0xNi4zNiAzLjg0bDMuNzkgMy43OS0xMC45NSAxMC45NS0zLjc5LTMuNzkgMTAuOTUtMTAuOTV6Ii8+PC9zdmc+") 0 24, pointer';


                    return;
                case 'positive': 
                case 'negative': 
                    canvas.style.cursor = 'pointer'; 
                    return; 
            } 
        } 
        canvas.style.cursor = (scale > 1 && !isDrawing && currentImage.src) ? 'grab' : 'default'; 
    }

    function updateSAMActionButtons() {
    if (currentWorkflowStage !== 'annotate_sam') return;

    // "Rückgängig"-Button-Status
    if (btnUndo) {
        btnUndo.disabled = (annotations.length === 0);
    }

    // "Segmentieren (SAM)"-Button-Status
    if (btnSegmentSam) {
        btnSegmentSam.disabled = (annotations.length === 0);
    }
    }

    // Funktion zum Wechseln zwischen Original- und Segmentierungsansicht
    function switchToView(viewType) {
        if (viewType === 'original') {
            isShowingSegmentation = false;
            if (btnViewOriginal) btnViewOriginal.classList.add('active');
            if (btnViewSegmentation) btnViewSegmentation.classList.remove('active');
            
            // Zeige das normale Bild mit Annotationen
            drawImageAndAnnotations();
        } else if (viewType === 'segmentation' && currentSegmentationUrl) {
            isShowingSegmentation = true;
            if (btnViewOriginal) btnViewOriginal.classList.remove('active');
            if (btnViewSegmentation) btnViewSegmentation.classList.add('active');
            
            // Lade die Segmentierung als Overlay über das Original
            loadSegmentationOverlay();
        }
        
        console.log(`Switched to view: ${viewType}, isShowingSegmentation: ${isShowingSegmentation}`);
    }

    function loadSegmentationOverlay() {
        if (!currentSegmentationUrl) return;
        
        const segmentationImage = new Image();
        segmentationImage.onload = function() {
            if (!ctx || !canvas) return;
            
            // Verwende die gleiche Zeichenlogik wie drawImageAndAnnotations für konsistente Transformation
            ctx.save();
            ctx.setTransform(1, 0, 0, 1, 0, 0); // Reset transform for clearing
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.translate(originX, originY);
            ctx.scale(scale, scale);
            
            try {
                // Original-Bild zeichnen
                if (currentImage && currentImage.complete) {
                    ctx.drawImage(currentImage, 0, 0, originalWidth, originalHeight);
                }
                
                // Segmentierung als farbiges Overlay darüber
                ctx.globalAlpha = 0.6;
                ctx.drawImage(segmentationImage, 0, 0, originalWidth, originalHeight);
                ctx.globalAlpha = 1.0;
                
                // Annotationen immer noch darüber anzeigen
                if (currentWorkflowStage === 'annotate_sam') {
                    annotations.forEach(anno => drawAnno(anno));
                    
                    // Zeichne auch die aktuelle Box-Vorschau, falls vorhanden
                    if (isDrawing && currentTool === 'box' && startX !== undefined) {
                        ctx.save();
                        drawBox([startX, startY, currentX, currentY], true);
                        ctx.restore();
                    }
                }
            } catch(e) {
                console.error("Fehler beim Zeichnen des Segmentierungs-Overlays:", e);
            }
            
            ctx.restore();
        };
        segmentationImage.onerror = function() {
            console.warn('Segmentierung konnte nicht geladen werden:', currentSegmentationUrl);
            // Zurück zur Original-Ansicht
            switchToView('original');
        };
        segmentationImage.src = `${currentSegmentationUrl}?t=${new Date().getTime()}`;
    }

    function drawImageAndAnnotations() {
        if (!currentImage || !currentImage.complete || currentImage.naturalWidth === 0 || !ctx) {
            clearCanvas();
            return;
        }
        
        // Wenn wir in der Segmentierungsansicht sind und eine Segmentierung vorhanden ist, 
        // lade das Overlay neu statt nur das Originalbild zu zeichnen
        if (isShowingSegmentation && currentSegmentationUrl && currentWorkflowStage === 'annotate_sam') {
            loadSegmentationOverlay();
            return;
        }
        
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0); // Reset transform for clearing
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.translate(originX, originY);
        ctx.scale(scale, scale);
        try {
            ctx.drawImage(currentImage, 0, 0, originalWidth, originalHeight);
        } catch(e) {
            console.error("Fehler beim Zeichnen des Hauptbildes:", e);
            ctx.restore(); // Stellen Sie sicher, dass der Kontext wiederhergestellt wird
            return;
        }

        if (currentWorkflowStage === 'annotate_sam') {
            annotations.forEach(anno => drawAnno(anno));
            if (isDrawing && currentTool === 'box' && startX !== undefined) {
                ctx.save(); // Zusätzliches Save/Restore für die Preview-Box
                drawBox([startX, startY, currentX, currentY], true);
                ctx.restore();
            } else if (isDrawing && currentTool === 'freehand' && freehandPath.length > 1) {
                ctx.save();
                drawFreehandPath(freehandPath, true);
                ctx.restore();
            }
        }
        drawVoxelSizePreviewOnContext(ctx);
        ctx.restore(); // Äußeres Restore
    }



    function drawFreehandPath(path, isPreview = false) {
        if (!ctx || !path || path.length < 2) return;
        
        ctx.save();
        ctx.lineWidth = Math.max(0.5, 2 / scale);
        ctx.strokeStyle = 'rgba(0, 80, 255, 1.0)'; // Blau für die Linie
        ctx.fillStyle = 'rgba(0, 80, 255, 0.2)'; // Hellblau für die Füllung
    
        if (isPreview) {
            ctx.setLineDash([5 / scale, 5 / scale]);
            ctx.strokeStyle = 'rgba(0, 120, 255, 0.7)'; // Etwas anderer Stil für die Vorschau
        }
    
        ctx.beginPath();
        ctx.moveTo(path[0][0], path[0][1]);
        for (let i = 1; i < path.length; i++) {
            ctx.lineTo(path[i][0], path[i][1]);
        }
        
        // Wenn es die finale Form ist (keine Vorschau), schließe den Pfad und fülle ihn
        if (!isPreview) {
            ctx.closePath(); // Dies zeichnet die Linie vom End- zum Startpunkt
            ctx.fill();
        }
        
        ctx.stroke(); // Zeichne immer die Umrandung
        ctx.restore();
    }
    // NEU: Funktion zum Zeichnen von Freihand-Pfaden
    function drawAnno(anno) {
        if (!anno || !anno.type) return;
        if (anno.type === 'box' && anno.data && anno.data.length === 4) {
            drawBox(anno.data);
        } else if (anno.type === 'freehand' && anno.path) { // Neuer `else if` Zweig
            drawFreehandPath(anno.path, false);
        } else if (anno.type === 'positive' && anno.data && anno.data.length === 2) {
            drawPoint(anno.data, 'rgba(0, 255, 0, 0.5)', 'green');
        } else if (anno.type === 'negative' && anno.data && anno.data.length === 2) {
            drawPoint(anno.data, 'rgba(255, 0, 0, 0.5)', 'darkred');
        }
    }
    function drawBox(coords, isPreview = false) {
        if (!ctx) return;
        
        // Speichere den aktuellen Zeichenkontext
        ctx.save();
        
        // Aktiviere Anti-Aliasing für glattere Linien
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        
        // Setze Stil für die Box
        ctx.strokeStyle = isPreview ? 'rgba(0, 120, 255, 0.8)' : 'rgba(0, 80, 255, 1.0)';
        ctx.lineWidth = Math.max(0.5, 2 / scale);
        
        // Gestrichelte Linie für die Vorschau
        if (isPreview) {
            ctx.setLineDash([5 / scale, 5 / scale]);
        } else {
            ctx.setLineDash([]);
        }
        
        // Berechne die Koordinaten der Box
        const x = Math.min(coords[0], coords[2]);
        const y = Math.min(coords[1], coords[3]);
        const w = Math.abs(coords[0] - coords[2]);
        const h = Math.abs(coords[1] - coords[3]);
        
        // Zeichne die Box
        ctx.strokeRect(x, y, w, h);
        
        // Zeichne kleine Marker an den Ecken für bessere Sichtbarkeit
        if (!isPreview && (w > 10 || h > 10)) {
            const markerSize = Math.max(2, 4 / scale);
            const corners = [
                [x, y],                 // Oben links
                [x + w, y],             // Oben rechts
                [x, y + h],             // Unten links
                [x + w, y + h]          // Unten rechts
            ];
            
            ctx.fillStyle = 'white';
            corners.forEach(corner => {
                ctx.beginPath();
                ctx.arc(corner[0], corner[1], markerSize, 0, Math.PI * 2);
                ctx.fill();
                ctx.stroke();
            });
        }
        
        // Setze die Linien-Dash-Einstellung zurück
        ctx.setLineDash([]);
        
        // Stelle den ursprünglichen Zeichenkontext wieder her
        ctx.restore();
        
        // Debug-Ausgabe der Box-Koordinaten
        if (!isPreview) {
            console.log(`Box drawn at: (${x.toFixed(1)}, ${y.toFixed(1)}) size: ${w.toFixed(1)}x${h.toFixed(1)}`);
        }
    }
    function drawPoint(coords, fillColor, strokeColor) {
    if (!ctx) return;

    // Angepasste Werte für bessere Sichtbarkeit:
    const outerRadiusBase = 10; // Basisgröße des äußeren Rings (verkleinert für bessere Präzision)
    const innerRadiusBase = 4;  // Basisgröße des inneren gefüllten Kreises (verkleinert für bessere Präzision)
    const lineWidthBase = 2;    // Basis-Linienstärke des äußeren Rings (verkleinert für bessere Präzision)

    // Mindestgrößen in Pixeln, damit die Punkte bei starkem Zoom nicht verschwinden
    const minPixelOuterRadius = 3; 
    const minPixelInnerRadius = 1.5;
    const minPixelLineWidth = 1;

    // Berechne die tatsächlichen Größen basierend auf dem aktuellen Zoom
    const r1 = Math.max(minPixelOuterRadius, outerRadiusBase / scale);
    const r2 = Math.max(minPixelInnerRadius, innerRadiusBase / scale);
    const currentLineWidth = Math.max(minPixelLineWidth, lineWidthBase / scale);
    
    // Speichere den aktuellen Zeichenkontext
    ctx.save();
    
    // Aktiviere Anti-Aliasing für glattere Kreise
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    
    // Zeichne den äußeren Ring
    ctx.beginPath();
    // Verwende präzise Positionierung mit Subpixel-Genauigkeit
    ctx.arc(coords[0], coords[1], r1, 0, Math.PI * 2);
    ctx.strokeStyle = fillColor;
    ctx.lineWidth = currentLineWidth;
    ctx.stroke();

    // Zeichne den inneren gefüllten Punkt
    ctx.beginPath();
    ctx.arc(coords[0], coords[1], r2, 0, Math.PI * 2);
    ctx.fillStyle = strokeColor;
    ctx.fill();
    
    // Zeichne ein kleines Fadenkreuz im Zentrum für bessere Präzision
    const crossSize = Math.max(1, 3 / scale);
    ctx.beginPath();
    ctx.moveTo(coords[0] - crossSize, coords[1]);
    ctx.lineTo(coords[0] + crossSize, coords[1]);
    ctx.moveTo(coords[0], coords[1] - crossSize);
    ctx.lineTo(coords[0], coords[1] + crossSize);
    ctx.strokeStyle = 'white';
    ctx.lineWidth = Math.max(0.5, 1 / scale);
    ctx.stroke();
    
    // Stelle den ursprünglichen Zeichenkontext wieder her
    ctx.restore();
    
    // Debug-Ausgabe der Punktkoordinaten
    console.log(`Point drawn at: (${coords[0].toFixed(1)}, ${coords[1].toFixed(1)})`);
    }

    function getMousePos(event) {
        if (!canvas || !canvas.width || !canvas.height) return {x:0, y:0};
        
        // Debug-Modus für Fehlerbehebung (kann bei Bedarf aktiviert werden)
        const debugMode = true; // Temporär aktiviert für Debugging
        
        // Hole die exakte Position und Größe des Canvas im Viewport
        const rect = canvas.getBoundingClientRect();
        
        // Berechne die Mausposition relativ zum Canvas-Element
        let mouseXOnElement = event.clientX - rect.left;
        let mouseYOnElement = event.clientY - rect.top;
        
        // Prüfe, ob die Maus innerhalb des Canvas ist
        const isInsideCanvas = mouseXOnElement >= 0 && mouseXOnElement <= rect.width && 
                              mouseYOnElement >= 0 && mouseYOnElement <= rect.height;
        
        // Wenn die Maus außerhalb des Canvas ist, begrenzen wir die Koordinaten
        if (!isInsideCanvas) {
            mouseXOnElement = Math.max(0, Math.min(rect.width, mouseXOnElement));
            mouseYOnElement = Math.max(0, Math.min(rect.height, mouseYOnElement));
        }
        
        // Berechne die Skalierungsfaktoren zwischen der internen Canvas-Größe und der angezeigten Größe
        const cssScaleX = canvas.width / rect.width;
        const cssScaleY = canvas.height / rect.height;
        
        // Berechne die Position auf dem internen Canvas (in Pixel-Koordinaten)
        const mouseXOnCanvasInternal = mouseXOnElement * cssScaleX;
        const mouseYOnCanvasInternal = mouseYOnElement * cssScaleY;
        
        // Berücksichtige Zoom und Pan, um die tatsächliche Position auf dem Bild zu erhalten
        const imgX = (mouseXOnCanvasInternal - originX) / scale;
        const imgY = (mouseYOnCanvasInternal - originY) / scale;
        
        // Debug-Ausgabe für Mauskoordinaten
        if (debugMode) {
            console.log(`Mouse: client(${event.clientX},${event.clientY}) | rect(${rect.width.toFixed(0)}x${rect.height.toFixed(0)}@${rect.left.toFixed(0)},${rect.top.toFixed(0)}) | mouseOnEl(${mouseXOnElement.toFixed(0)},${mouseYOnElement.toFixed(0)}) | cssScales(${cssScaleX.toFixed(2)},${cssScaleY.toFixed(2)}) | mouseOnInternal(${mouseXOnCanvasInternal.toFixed(0)},${mouseYOnCanvasInternal.toFixed(0)}) | origin(${originX.toFixed(0)},${originY.toFixed(0)}) scale:${scale.toFixed(2)} | imgCoords(${imgX.toFixed(0)},${imgY.toFixed(0)})`);
        }
        
        return { x: imgX, y: imgY };
    }

    if (canvas) {
        canvas.addEventListener('mousedown', (e) => {
            if (!currentImage.src || currentWorkflowStage === 'finished') return;
            const pos = getMousePos(e);
            
            // Panning-Logik (unverändert)
            if (e.button === 1 || (e.button === 0 && currentTool === null && scale > 1.01) ) { 
                isPanning = true;
                panStartX = e.clientX - originX;
                panStartY = e.clientY - originY;
                canvas.style.cursor = 'grabbing';
                e.preventDefault();
            } else if (e.button === 0 && currentWorkflowStage === 'annotate_sam') {
                
                if (currentTool === 'box') {
                    const i = annotations.findIndex(a => a.type === 'box');
                    if (i !== -1) annotations.splice(i, 1);
                    isDrawing = true;
                    startX = Math.max(0, Math.min(originalWidth, pos.x));
                    startY = Math.max(0, Math.min(originalHeight, pos.y));
                    currentX = startX;
                    currentY = startY;
                    e.preventDefault();
                } else if (currentTool === 'freehand') { // KORRIGIERTE Freihand-Logik
                    // Bestehende Box- oder Freihand-Annotation entfernen
                    const i = annotations.findIndex(a => a.type === 'box' || a.type === 'freehand');
                    if (i !== -1) annotations.splice(i, 1);
                    
                    isDrawing = true;
                    freehandPath = []; // Pfad zurücksetzen
                    freehandPath.push([pos.x, pos.y]);
                    drawImageAndAnnotations(); // Wichtig: Leinwand neu zeichnen
                    e.preventDefault();
                } else if (currentTool === 'positive' || currentTool === 'negative') {
                    const x = Math.max(0, Math.min(originalWidth, pos.x));
                    const y = Math.max(0, Math.min(originalHeight, pos.y));
                    annotations.push({type: currentTool, data: [x, y]});
                    if(btnUndo) btnUndo.disabled = false;
                    drawImageAndAnnotations();
                    updateSAMActionButtons();
                }
            }
        });
    
        canvas.addEventListener('mousemove', (e) => {
            if (!isDrawing && !isPanning) return;
            const pos = getMousePos(e);
    
            if (isDrawing && currentTool === 'box' && currentWorkflowStage === 'annotate_sam') {
                currentX = Math.max(0, Math.min(originalWidth, pos.x));
                currentY = Math.max(0, Math.min(originalHeight, pos.y));
                drawImageAndAnnotations();
            } else if (isDrawing && currentTool === 'freehand' && currentWorkflowStage === 'annotate_sam') { // KORRIGIERTE Freihand-Logik
                freehandPath.push([pos.x, pos.y]);
                drawImageAndAnnotations(); // Live-Vorschau des Pfades zeichnen
            } else if (isPanning) {
                originX = e.clientX - panStartX;
                originY = e.clientY - panStartY;
                drawImageAndAnnotations();
                if (currentWorkflowStage === 'annotate_thresh') {
                    updateThresholdPreview();
                }
            }
        });
    
        canvas.addEventListener('mouseup', (e) => {
            if (e.button !== 0) { isPanning = false; setCanvasCursor(); return; }
    
            if (isPanning) {
                 isPanning = false;
                 setCanvasCursor();
                 e.preventDefault();
            } else if (isDrawing) {
                if (currentTool === 'box') {
                    const fX = Math.max(0, Math.min(originalWidth, getMousePos(e).x));
                    const fY = Math.max(0, Math.min(originalHeight, getMousePos(e).y));
                    const bC = [Math.round(Math.min(startX,fX)), Math.round(Math.min(startY,fY)), Math.round(Math.max(startX,fX)), Math.round(Math.max(startY,fY))];
                    if (Math.abs(bC[0]-bC[2]) > 3 && Math.abs(bC[1]-bC[3]) > 3) {
                        annotations.push({type: 'box', data: bC });
                        if(btnUndo) btnUndo.disabled = false;
                    }
                } else if (currentTool === 'freehand') { // KORRIGIERTE Freihand-Logik
                    if (freehandPath.length > 2) {
                        let minX = freehandPath[0][0], minY = freehandPath[0][1];
                        let maxX = freehandPath[0][0], maxY = freehandPath[0][1];
                        
                        for (let i = 1; i < freehandPath.length; i++) {
                            minX = Math.min(minX, freehandPath[i][0]);
                            minY = Math.min(minY, freehandPath[i][1]);
                            maxX = Math.max(maxX, freehandPath[i][0]);
                            maxY = Math.max(maxY, freehandPath[i][1]);
                        }
                        const boundingBox = [Math.round(minX), Math.round(minY), Math.round(maxX), Math.round(maxY)];
    
                        // Annotation mit Pfad zum Zeichnen und Box für die Datenübertragung
                        annotations.push({
                            type: 'freehand',
                            path: [...freehandPath], 
                            data: boundingBox // Bounding Box für SAM
                        });
                        
                        if(btnUndo) btnUndo.disabled = false;
                        console.log("Freihandpfad abgeschlossen. Bounding Box:", boundingBox);
                    }
                    freehandPath = []; // Temporären Pfad nach Nutzung leeren
                }
                isDrawing = false;
                startX = undefined;
                drawImageAndAnnotations();
                updateSAMActionButtons();
            }
        });
        canvas.addEventListener('mouseout', (e) => {
            if (isDrawing && currentTool === 'box') {
                isDrawing = false; startX = undefined;
                // debugClickPos = null; // Optional
                drawImageAndAnnotations();
            }
            if (isDrawing && currentTool === 'freehand') {
                isDrawing = false; freehandPath = [];
                drawImageAndAnnotations();
            }
            if (isPanning) {
                isPanning = false; setCanvasCursor();
            }
        });
        canvas.addEventListener('wheel', (e) => {
            if (!currentImage.src || currentIndex < 0 || currentWorkflowStage === 'finished' || !canvas.width || !canvas.height) return;
            e.preventDefault();
            
            // Hole die exakte Position und Größe des Canvas im Viewport
            const rect = canvas.getBoundingClientRect();
            
            // Berechne die Mausposition relativ zum Canvas-Element
            let mouseXOnElement = e.clientX - rect.left;
            let mouseYOnElement = e.clientY - rect.top;
            
            // Berechne die Skalierungsfaktoren zwischen der internen Canvas-Größe und der angezeigten Größe
            const cssScaleX = canvas.width / rect.width;
            const cssScaleY = canvas.height / rect.height;
            
            // Berechne die Position auf dem internen Canvas (in Pixel-Koordinaten)
            const mouseXOnCanvasInternal = mouseXOnElement * cssScaleX;
            const mouseYOnCanvasInternal = mouseYOnElement * cssScaleY;
            
            // Mausposition im aktuellen Bildkoordinatensystem (vor Zoomänderung)
            const mXb = (mouseXOnCanvasInternal - originX) / scale;
            const mYb = (mouseYOnCanvasInternal - originY) / scale;

            // Zoomrichtung bestimmen (nach oben = reinzoomen, nach unten = rauszoomen)
            const d = e.deltaY > 0 ? -1 : 1; // Zoomrichtung
            
            // Neuen Zoom-Faktor berechnen
            let newScale = scale * Math.pow(ZOOM_FACTOR, d);
            
            // Zoom-Faktor auf sinnvolle Grenzen beschränken
            newScale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, newScale));
            
            // Neue Origin berechnen, damit der Punkt unter der Maus fixiert bleibt
            // Dies ist wichtig für ein natürliches Zoom-Verhalten
            originX = mouseXOnCanvasInternal - mXb * newScale;
            originY = mouseYOnCanvasInternal - mYb * newScale;
            
            // Neuen Zoom-Faktor anwenden
            scale = newScale;
            
            // Debug-Ausgabe
            console.log(`Zoom: scale=${scale.toFixed(2)}, origin=(${originX.toFixed(0)},${originY.toFixed(0)}), mouse=(${mXb.toFixed(0)},${mYb.toFixed(0)})`);

            // Cursor und Anzeige aktualisieren
            setCanvasCursor();
            drawImageAndAnnotations();
            if (currentWorkflowStage === 'annotate_thresh' && currentRawPixelData) {
                updateThresholdPreview();
            }
        }, { passive: false });
    }

    toolButtonsSAM.forEach(b => {
        if(b) b.addEventListener('click', async () => { 
            if(b.classList.contains('active'))setTool(null); 
            else setTool(b.dataset.tool);})});
        if(btnUndo) btnUndo.addEventListener('click', () => { 
            if(currentWorkflowStage !== 'annotate_sam' || annotations.length === 0) 
                return; annotations.pop(); 
            if(annotations.length===0 && btnUndo)btnUndo.disabled=true; debugClickPos = null; 
            drawImageAndAnnotations(); 
            if(statusMessage)statusMessage.textContent="Letzte SAM-Aktion rückgängig.";});
        if(btnReset) btnReset.addEventListener('click', async () => { 
            if(currentWorkflowStage !== 'annotate_sam') 
                return; 
        if(await showConfirmationModal("Annotationen zurücksetzen?", "Sollen wirklich alle Annotationen für diesen Slice zurückgesetzt werden?")){
        resetSamAnnotationsAndZoom(); 
        drawImageAndAnnotations(); 
        if(statusMessage)statusMessage.textContent="SAM-Annotationen zurückgesetzt.";
        }});

    if(btnApplyWl) btnApplyWl.addEventListener('click', () => {
        if(currentWorkflowStage!=='wl_select')
            return;
        fixedWL=currentWL; fixedWW=currentWW;

        // Spielmodus: Überspringe Methodenwahl und gehe direkt zu SAM
        if (window.gameMode) {
            currentWorkflowStage = 'annotate_sam';
            segmentationMethod = 'sam';
            console.log('Spielmodus: Überspringe Methodenwahl, gehe direkt zu SAM-Annotation');
        } else {
            currentWorkflowStage = 'method_select';
            segmentationMethod = null;
        }

        updateUIForStage();
        if(statusMessage) {
            if (window.gameMode) {
                statusMessage.textContent = "Spielmodus: Segmentieren Sie mit SAM.";
            } else {
                statusMessage.textContent = "Segmentierungsmethode wählen.";
            }
        }
    });
    if(btnBackToWlFromSelect) btnBackToWlFromSelect.addEventListener('click', () => { 
        if(currentWorkflowStage!=='method_select')return; 
        currentWorkflowStage='wl_select'; 
        segmentationMethod=null; 
        fixedWL=null; 
        fixedWW=null; 
        savedIndicesPerAxis={}; 
        allAnnotations={}; 
        sam2FixatedAxis=null; 
        savedMethodPerSlice={}; 
        debugClickPos = null; 
        updateUIForStage(); 
        updateWLWWControls(); 
        updateProgressDisplay(); 
        if(fileType==='dicom_series')highlightActiveListItem(); 
        else updateSliceSlider(); 
        if(statusMessage)statusMessage.textContent="Fensterung anpassen.";});
        
    if (btnSelectSam) {
    btnSelectSam.addEventListener('click', async () => { // Funktion ist async
        if (currentWorkflowStage !== 'method_select') return;

        // DIREKT INTEGRIERT: Session auf dem Server zurücksetzen
        try {
            const response = await fetch('/set_method', { method: 'POST' });
            if (!response.ok) {
                console.error('Server-Fehler beim Zurücksetzen der Session.');
                alert('Ein Server-Fehler ist aufgetreten. Bitte versuchen Sie es erneut.');
                return; // Wichtig: Bei Fehler abbrechen
            }
            console.log('Server-seitige Segmentierungs-Session für SAM zurückgesetzt.');
        } catch (error) {
            console.error('Netzwerkfehler beim Zurücksetzen der Session:', error);
            alert('Ein Netzwerkfehler ist aufgetreten. Bitte überprüfen Sie die Verbindung.');
            return; // Wichtig: Bei Fehler abbrechen
        }

        // Kompletter Reset für den SAM-Zustand (bleibt wie es war)
        console.log("SAM-Methode ausgewählt. Setze Annotations-Zustand zurück.");
        annotations = [];
        allAnnotations = {};
        lastSupportMaskFiles = null;
        savedIndicesPerAxis = {};
        savedMethodPerSlice = {};
        sam2FixatedAxis = null;
        currentSegmentationUrl = null;
        isShowingSegmentation = false;
        if (btnUndo) btnUndo.disabled = true;
        if (btnViewOriginal) btnViewOriginal.classList.add('active');
        if (btnViewSegmentation) {
            btnViewSegmentation.classList.remove('active');
            btnViewSegmentation.disabled = true;
        }

        // UI-Logik (bleibt wie es war)
        segmentationMethod = 'sam';
        currentWorkflowStage = 'annotate_sam';
        loadSamAnnotationsForIndex(currentAxis, currentIndex);
        resetZoomAndPan();
        updateUIForStage();
        drawImageAndAnnotations();
        if(statusMessage) statusMessage.textContent = isVolumetric() ? "SAM: Bitte Achse für Annotation wählen oder direkt annotieren." : "SAM: Bereit für Annotation.";
    });
}
    if (btnSelectThresh) {
    btnSelectThresh.addEventListener('click', async () => { // Funktion ist async
        if (currentWorkflowStage !== 'method_select') return;
        if (!currentRawPixelData) {
            alert("Keine Rohdaten für Thresholding verfügbar.");
            return;
        }

        // DIREKT INTEGRIERT: Session auf dem Server zurücksetzen
        try {
            const response = await fetch('/set_method', { method: 'POST' });
            if (!response.ok) {
                console.error('Server-Fehler beim Zurücksetzen der Session.');
                alert('Ein Server-Fehler ist aufgetreten. Bitte versuchen Sie es erneut.');
                return; // Wichtig: Bei Fehler abbrechen
            }
            console.log('Server-seitige Segmentierungs-Session für Thresholding zurückgesetzt.');
        } catch (error) {
            console.error('Netzwerkfehler beim Zurücksetzen der Session:', error);
            alert('Ein Netzwerkfehler ist aufgetreten. Bitte überprüfen Sie die Verbindung.');
            return; // Wichtig: Bei Fehler abbrechen
        }
        
        updateThresholdSliderMinMax(currentRawPixelData);
        // UI-Logik (bleibt wie es war)
        segmentationMethod = 'threshold';
        currentWorkflowStage = 'annotate_thresh';
        resetZoomAndPan();
        updateUIForStage();
        if (currentRawPixelData) updateThresholdPreview();
        if (statusMessage) statusMessage.textContent = "Thresholding.";
    });
}
    if (btnBackToMethodSelectFromSam) {
    btnBackToMethodSelectFromSam.addEventListener('click', async () => { // Funktion wird async
        if (currentWorkflowStage !== 'annotate_sam') return;

        if (await showConfirmationModal("Zurück zur Methodenauswahl?", "Der gesamte Fortschritt für die SAM-Methode wird zurückgesetzt. Sind Sie sicher?")) {
        await resetSegmentationSession();
            // DIREKT INTEGRIERT: Session auf dem Server zurücksetzen
            try {
                const response = await fetch('/set_method', { method: 'POST' });
                if (!response.ok) {
                    console.error('Server-Fehler beim Zurücksetzen der Session.');
                    alert('Ein Server-Fehler ist aufgetreten. Bitte versuchen Sie es erneut.');
                    return; // Wichtig: Bei Fehler abbrechen
                }
                console.log('Server-seitige Session für Methodenwechsel zurückgesetzt.');
            } catch (error) {
                console.error('Netzwerkfehler beim Zurücksetzen der Session:', error);
                alert('Ein Netzwerkfehler ist aufgetreten. Bitte überprüfen Sie die Verbindung.');
                return; // Wichtig: Bei Fehler abbrechen
            }

            // Umfassender Reset (bleibt wie es war)
            console.log("Verlasse SAM-Modus. Setze Annotations-Zustand zurück.");
            annotations = [];
            allAnnotations = {};
            lastSupportMaskFiles = null;
            savedIndicesPerAxis = {};
            savedMethodPerSlice = {};
            sam2FixatedAxis = null;
            debugClickPos = null;
            currentSegmentationUrl = null;
            isShowingSegmentation = false;
            if (btnUndo) btnUndo.disabled = true;
            if (btnViewOriginal) btnViewOriginal.classList.add('active');
            if (btnViewSegmentation) {
                btnViewSegmentation.classList.remove('active');
                btnViewSegmentation.disabled = true;
            }

            // UI-Logik (bleibt wie es war)
            currentWorkflowStage = 'method_select';
            segmentationMethod = null;
            updateUIForStage();
            updateProgressDisplay();
            if (statusMessage) statusMessage.textContent = "Segmentierungsmethode wählen.";
        }
    });
}
    if (btnBackToMethodSelectFromThresh) {
    btnBackToMethodSelectFromThresh.addEventListener('click', async () => { // Funktion wird async
        if (currentWorkflowStage !== 'annotate_thresh') return;
        await resetSegmentationSession();
        // DIREKT INTEGRIERT: Session auf dem Server zurücksetzen
        try {
            const response = await fetch('/set_method', { method: 'POST' });
            if (!response.ok) {
                console.error('Server-Fehler beim Zurücksetzen der Session.');
                alert('Ein Server-Fehler ist aufgetreten. Bitte versuchen Sie es erneut.');
                return; // Wichtig: Bei Fehler abbrechen
            }
            console.log('Server-seitige Session für Methodenwechsel zurückgesetzt.');
        } catch (error) {
            console.error('Netzwerkfehler beim Zurücksetzen der Session:', error);
            alert('Ein Netzwerkfehler ist aufgetreten. Bitte überprüfen Sie die Verbindung.');
            return; // Wichtig: Bei Fehler abbrechen
        }

        // UI-Logik (bleibt wie es war)
        currentWorkflowStage = 'method_select';
        segmentationMethod = null;
        clearPreviewCanvas();
        updateUIForStage();
        if (statusMessage) statusMessage.textContent = "Segmentierungsmethode wählen.";
    });
}

    // Toggle-Button Event-Listener
    if(btnViewOriginal) btnViewOriginal.addEventListener('click', async () => { if(currentWorkflowStage === 'annotate_sam') switchToView('original'); });
    if(btnViewSegmentation) btnViewSegmentation.addEventListener('click', () => { if(currentWorkflowStage === 'annotate_sam' && !btnViewSegmentation.disabled) switchToView('segmentation'); });

    // Next-Button Event-Listener
    if(btnNextSam) btnNextSam.addEventListener('click', () => {
        if (currentWorkflowStage !== 'annotate_sam') return;

        const navAxis = sam2FixatedAxis || currentAxis;
        const totalForNav = isVolumetric() ? (sliceCounts[navAxis] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);

        // Speichere die aktuellen Annotationen, bevor zum nächsten Bild gewechselt wird.
        // Dies stellt sicher, dass Zeichnungen nicht verloren gehen, wenn man hin- und herspringt.
        saveCurrentSamAnnotations();

        // Navigation zum nächsten Slice (ohne den aktuellen als 'bearbeitet' zu markieren)
        if (currentIndex < totalForNav - 1) {
            loadImage(currentIndex + 1, navAxis);
        } else if (currentIndex === totalForNav - 1 && totalForNav > 0) {
            // Wenn man auf dem letzten Slice ist und "Weiter" klickt,
            // wird die Finalisierungssequenz ausgelöst.
            loadImage(currentIndex + 1, navAxis);
        }
    });

    if (btnFinalizeSam) btnFinalizeSam.addEventListener('click', () => { const axisToFinalize = sam2FixatedAxis || (isVolumetric() ? currentAxis : 'axial'); triggerFinishSequence('sam', axisToFinalize); });
    if (btnFinalizeThresh) btnFinalizeThresh.addEventListener('click', () => { triggerFinishSequence('threshold', currentAxis); });

    if (btnSelectRangeTemplate) {
    btnSelectRangeTemplate.addEventListener('click', () => {
        if (!templateSliceFromInput || !templateSliceToInput || !templateImageListDiv) return;

        const fromSliceUser = parseInt(templateSliceFromInput.value, 10); // 1-basiert vom User
        const toSliceUser = parseInt(templateSliceToInput.value, 10);     // 1-basiert vom User

        if (isNaN(fromSliceUser) || isNaN(toSliceUser) || fromSliceUser < 1 || toSliceUser < fromSliceUser) {
            alert("Bitte geben Sie einen gültigen Slice-Bereich ein (Von <= Bis).");
            return;
        }
        const axisForTemplate = sam2FixatedAxis || currentAxis;
        const maxSlices = isVolumetric() ? (sliceCounts[axisForTemplate] || 0) : dicomFilenames.length;

        const fromIndex = Math.max(0, fromSliceUser - 1); // Konvertiere zu 0-basiertem Index
        const toIndex = Math.min(maxSlices - 1, toSliceUser - 1); // Konvertiere zu 0-basiertem Index

        if (fromIndex > toIndex) {
             alert("Ungültiger Bereich oder keine Slices vorhanden.");
             return;
        }

        templateImageListDiv.querySelectorAll('.panel-chk').forEach(chk => {
            const chkIndex = parseInt(chk.dataset.index, 10);
            chk.checked = (chkIndex >= fromIndex && chkIndex <= toIndex);
        });
        updateConfirmButtonText();
        if(statusMessage) statusMessage.textContent = `Slices ${fromSliceUser} bis ${toSliceUser} markiert.`;
    });
}

    async function triggerFinishSequence(method, axisForMask = currentAxis, skipConfirmation = false) {
    console.log(`[TRIGGER FINISH START] Methode: ${method}, Achse: ${axisForMask}, SkipConfirm: ${skipConfirmation}`);
    if (currentWorkflowStage === 'finished') {
        console.log("[TRIGGER FINISH] Bereits im Finished-Stage. Abbruch.");
        return;
    }

    // Prüfe, ob es Slices gibt, die Annotationen haben, aber noch nicht segmentiert wurden
    const unsegmentedSlices = [];
    if (method === 'sam' && allAnnotations[axisForMask]) {
        const totalSlicesOnAxis = isVolumetric() ? (sliceCounts[axisForMask] || 0) : dicomFilenames.length;
        for (let i = 0; i < totalSlicesOnAxis; i++) {
            if (allAnnotations[axisForMask][i] && allAnnotations[axisForMask][i].length > 0) {
                const savedSet = savedIndicesPerAxis[axisForMask] || new Set();
                const savedMethod = savedMethodPerSlice[`${axisForMask}_${i}`];
                if (!savedSet.has(i) || (savedMethod !== 'sam' && savedMethod !== 'sam_template_applied')) {
                    unsegmentedSlices.push(i);
                }
            }
        }
    }

    if (unsegmentedSlices.length > 0) {
        console.log(`[TRIGGER FINISH] Automatische Segmentierung von ${unsegmentedSlices.length} nicht-segmentierten Slices...`);
        await segmentMultipleSlices(unsegmentedSlices, axisForMask);
    }

    // KORREKTUR: Prüfe, ob die Bestätigung übersprungen werden soll
    let createMask = false;
    if (skipConfirmation) {
        createMask = true;
        console.log("[TRIGGER FINISH] Bestätigung übersprungen, erstelle 3D-Maske automatisch.");
    } else {
        createMask = await showConfirmationModal(
            "Bearbeitung abschließen?", 
            `Soll die Bearbeitung für die Methode '${method}'${isVolumetric() && method === 'sam' ? ` (Achse: ${axisForMask})` : ''} abgeschlossen und eine 3D-Maske erstellt werden?`
        );
    }

    if (!createMask) {
        console.log("[TRIGGER FINISH] Benutzer hat 'Nein' zur 3D-Maske gewählt oder Aktion wurde abgebrochen.");
        currentWorkflowStage = 'finished';
        updateUIForStage();
        if(statusMessage) statusMessage.textContent = `Segmentierung abgeschlossen (ohne 3D-Maske).`;
        return;
    }

    showBlockingLoader(`Erstelle finale 3D-Maske für Achse '${axisForMask}' (${method}). Bitte warten...`, { cancellable: true });
    let materialDensity = null;
    if (method === 'sam' && inputMaterialDensitySam && inputMaterialDensitySam.value) {
        materialDensity = parseFloat(inputMaterialDensitySam.value);
    } else if (method === 'threshold' && inputMaterialDensityThresh && inputMaterialDensityThresh.value) {
        materialDensity = parseFloat(inputMaterialDensityThresh.value);
    }

    let payload = { 
        method: method, 
        fill_holes: method === 'sam' ? (cbFillHolesSam && cbFillHolesSam.checked) : (cbFillHolesThresh && cbFillHolesThresh.checked), 
        remove_outliers: method === 'sam' ? (cbRemoveOutliersSam && cbRemoveOutliersSam.checked) : (cbRemoveOutliersThresh && cbRemoveOutliersThresh.checked), 
        min_outlier_size: method === 'sam' ? (inputMinOutlierSizeSam && inputMinOutlierSizeSam.value) : (inputMinOutlierSizeThresh && inputMinOutlierSizeThresh.value),
        ww: fixedWW !== null ? fixedWW : currentWW,
        wl: fixedWL !== null ? fixedWL : currentWL,
        material_density: materialDensity
    };

    let endpoint = ''; 
    if (method === 'sam') {
        endpoint = '/create_3d_mask_sam';
        payload.axis = axisForMask;
        payload.all_annotations = allAnnotations;
        // Falls zuletzt Few-Shot/S1SFQ/Memory gelaufen ist: nur diese Support-Slices
        // in der segmentation_parameters.json dokumentieren.
        if (lastSupportMaskFiles && lastSupportMaskFiles.length) {
            payload.support_mask_files = lastSupportMaskFiles;
        }
    } else if (method === 'threshold') {
        endpoint = '/create_nifti_threshold';
        payload.lower = currentLowerThresh;
        payload.upper = currentUpperThresh;
        payload.view_axis = currentAxis;
        addSliceRangeToPayload(payload);
    } else {
        hideBlockingLoader();
        return; 
    }

    if (endpoint) {
        try {
            const response = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal: currentAbortController ? currentAbortController.signal : undefined });
            const result = await response.json();
            console.log("[TRIGGER FINISH] Backend-Antwort empfangen:", result);
            if (!response.ok || result.error) { throw new Error(result.error || `Serverfehler ${response.status}`); }

            if (result.success && result.nifti_url) {
                let statusText = `3D Maske "${result.nifti_filename}" erstellt! Lade 3D Ansicht...`;
                if (result.active_overlay_source) {
                    finalOverlaySource = result.active_overlay_source;
                    console.log("Finale Overlays werden aus Quelle geladen:", finalOverlaySource);
                } else {
                    finalOverlaySource = 'manual'; // Fallback
                }
                // Finale Overlays setzen (Few-Shot priorisiert)
                if (Array.isArray(result.final_overlay_files) && result.final_overlay_files.length > 0) {
                    finalOverlayFiles = result.final_overlay_files;
                } else {
                    finalOverlayFiles = [];
                }
                updateWeightInfoDisplay(result);
                if (result.message && result.message.includes("archiviert")) {
                    statusText += " Ergebnisse wurden archiviert.";
                }
                if(statusMessage) statusMessage.textContent = statusText;
                const totalForAxis = isVolumetric() ? (sliceCounts[axisForMask] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
                const weightData = {
                    volume_cm3: result.volume_cm3,
                    weight_g: result.weight_g,
                    material_density: result.material_density
                };
                await waitForNiivueAndInit(result.nifti_url, totalForAxis, weightData);

                // Spielmodus: Automatischer GT-Vergleich und Ranglisten-Anzeige
                if (window.gameMode && result.game_mode_gt_comparison) {
                    console.log("Spielmodus: Automatischer GT-Vergleich erkannt, zeige Ergebnisse");
                    handleGameModeCompletion(result.game_mode_gt_comparison, weightData);
                }
            } else {
                throw new Error(result.error || "NIfTI-Erstellung fehlgeschlagen (keine URL).");
            }
        } catch (error) {
            if (error && error.name === 'AbortError') {
                console.log("[TRIGGER FINISH] Vom Nutzer abgebrochen – zurück in die Annotation.");
                currentWorkflowStage = (method === 'threshold') ? 'annotate_thresh' : 'annotate_sam';
                if (statusMessage) statusMessage.textContent = "Abgebrochen.";
                updateUIForStage();
                return;   // finally blendet den Loader aus
            }
            console.error(`[TRIGGER FINISH ERROR] Fehler bei NIfTI-Erstellung (${method}):`, error);
            if(statusMessage) statusMessage.textContent = `Fehler 3D Maske (${method}): ${error.message}`;
            const totalForAxis = isVolumetric() ? (sliceCounts[axisForMask] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
            currentIndex = totalForAxis > 0 ? totalForAxis : 0;
            currentWorkflowStage = 'finished';
            if (niivueViewer) { try { niivueViewer.detach(); } catch(e){} niivueViewer = null; }
            updateUIForStage();
        } finally {
            hideBlockingLoader();
            console.log("[TRIGGER FINISH END] Sequenz beendet.");
        }
    } else {
        hideBlockingLoader();
    }
}
    // Funktion waitForNiivueAndInit Signatur ändern
    async function waitForNiivueAndInit(niftiUrl, numTotalSlicesForPrevStage, weightData = null) {
        if (typeof niftiUrl !== 'string' || !niftiUrl) {
            console.error(`[WaitNiivue] FATAL: Funktion wurde mit ungültiger URL aufgerufen:`, niftiUrl);
            if(statusMessage) statusMessage.textContent = `Interner Fehler: Ungültige NIfTI-URL erhalten.`;
            if(placeholderText3d) {
                placeholderText3d.style.display = 'block';
                placeholderText3d.textContent = "Interner Fehler: Ungültige URL für 3D-Ansicht. Bitte die Entwicklerkonsole prüfen.";
            }
            return;
        }

        return new Promise(async (resolve) => {
            let attempts = 0;
            const maxAttempts = 20;
            const interval = 100;
            
            console.log("[WaitNiivue] Starte Warte-Schleife auf niivue...");

            const checkAndInit = async () => {
                console.log(`[WaitNiivue] Versuch ${attempts + 1}/${maxAttempts}. typeof niivue: ${typeof niivue}, typeof niivue?.Niivue: ${typeof niivue?.Niivue}`);

                if (typeof niivue !== 'undefined' && typeof niivue.Niivue === 'function' && previewAreaContainer && preview3dArea && placeholderText3d && niftiViewerCanvas) {
                    
                    clearInterval(initInterval);
                    console.log("[WaitNiivue] Niivue bereit. Setze finalen Status 'finished' und aktualisiere UI.");
                    if(statusMessage) statusMessage.textContent = "3D-Viewer wird initialisiert...";
                    currentIndex = numTotalSlicesForPrevStage;
                    currentWorkflowStage = 'finished';
                    updateUIForStage();

                    // Finale Viewer-Bereiche zeigen und Toggle initialisieren
                    // previewAreaContainer und finalViewToggleContainer werden bereits durch updateUIForStage() kontrolliert
                    if (preview3dArea) preview3dArea.classList.remove('hidden');
                    preview3dArea.classList.remove('empty-preview');
                    if (placeholderText3d) placeholderText3d.style.display = 'none';
                    if (niftiViewerCanvas) niftiViewerCanvas.style.display = 'block';
                    
                    const urlParts = window.location.href.split('/');
                    const queryParams = new URLSearchParams(window.location.search);
                    const niftiFilename = queryParams.get('nifti');
                    let archiveId = "";
                    if (niftiFilename) {
                        const archiveIdMatch = niftiFilename.match(/(\d{8}_\d{6})/);
                        if (archiveIdMatch && archiveIdMatch[1]) {
                            archiveId = archiveIdMatch[1];
                        }
                    }
                    
                    if (!archiveId) {
                        const now = new Date();
                        archiveId = now.toISOString().replace(/[-:T]/g, '').slice(0, 15);
                    }
                    // 2D-Viewer initialisieren (falls Overlays vorhanden)
                    setupFinalOverlayViewer();
                    // Default: 3D anzeigen, 2D ausblenden
                    setFinalView('3d');
                    if (resultActionsDiv) {
                        resultActionsDiv.classList.remove('hidden');
                        resultActionsDiv.dataset.archiveId = archiveId;
                        
                        // --- ANFANG DER KORREKTUR ---
                        // Die Logik hier ist jetzt identisch zur `updateWeightInfoDisplay` Funktion
                        // und prüft jeden Wert einzeln.
                        const weightInfoDiv = document.getElementById('weight-info');
                        const volumeDisplay = document.getElementById('volume-display');
                        const weightDisplay = document.getElementById('weight-display');
                        const densityDisplay = document.getElementById('density-display');

                        if (weightInfoDiv && volumeDisplay && weightDisplay && densityDisplay) {
                            if (weightData && weightData.volume_cm3 != null) {
                                volumeDisplay.textContent = weightData.volume_cm3.toFixed(2);
                            } else {
                                volumeDisplay.textContent = "-";
                            }
                            if (weightData && weightData.weight_g != null) {
                                weightDisplay.textContent = weightData.weight_g.toFixed(2);
                            } else {
                                weightDisplay.textContent = "-";
                            }
                            if (weightData && weightData.material_density != null) {
                                densityDisplay.textContent = weightData.material_density;
                            } else {
                                densityDisplay.textContent = "-";
                            }
                            console.log("[WEIGHT INFO] Korrekte Anzeige in waitForNiivueAndInit:", weightData);
                        }
                        // --- ENDE DER KORREKTUR ---
                    }

                    if (niivueViewer) {
                        try {
                            console.log("[WaitNiivue] Alte niivueViewer Instanz gefunden. Versuche detach().");
                            niivueViewer.detach();
                            console.log("[WaitNiivue] Alte Instanz detached.");
                        } catch (e) {
                            console.warn("[WaitNiivue] Fehler beim detach des alten Niivue Viewers:", e);
                        }
                        niivueViewer = null;
                        console.log("[WaitNiivue] Alte niivueViewer Instanz auf null gesetzt.");
                    }

                    try {
                        console.log(`[WaitNiivue] Erstelle neue Niivue-Instanz und lade Volumen von: ${niftiUrl}`);
                        niivueViewer = new niivue.Niivue({
                            isResizeCanvas: true,
                            isHighResolutionCapable: false,
                            logLevel: 'info',
                            isTimeline: false,
                            isOrientCube: true,                   // Orientierungswürfel (L/R, A/P, S/I) im 3D-Render
                            backColor: [0.16, 0.17, 0.20, 1.0]    // dezent heller/neutraler statt tiefschwarz
                        });
                        await niivueViewer.attachToCanvas(niftiViewerCanvas);
                        const absoluteNiftiUrl = new URL(niftiUrl, window.location.origin).href;
                        console.log(`[WaitNiivue] Lade Volumen von relativer URL: '${niftiUrl}' und absoluter URL: '${absoluteNiftiUrl}'`);
                        const volume = { url: absoluteNiftiUrl };
                        if(statusMessage) statusMessage.textContent = "3D-Maske wird geladen...";
                        await niivueViewer.loadVolumes([volume]);
                        console.log("[WaitNiivue] Volumen erfolgreich geladen.");
                        if(statusMessage) statusMessage.textContent = "3D-Ansicht wird konfiguriert...";
                        if (niivueViewer.volumes.length > 0) {
                            niivueViewer.volumes[0].colormap = "green";
                            niivueViewer.volumes[0].opacity = 0.35; // etwas transparenter für bessere Überlagerung
                            niivueViewer.setSliceType(niivue.SLICE_TYPE.RENDER);
                            niivueViewer.updateGLVolume();
                            if(statusMessage) statusMessage.textContent = "3D-Ansicht bereit! Segmentierung erfolgreich abgeschlossen.";
                        }
                    } catch (error) {
                        console.error("[WaitNiivue] FEHLER bei der Initialisierung von Niivue oder beim Laden des Volumens:", error);
                        if(statusMessage) statusMessage.textContent = `Fehler beim Anzeigen der 3D-Maske: ${error.message}`;
                        if(placeholderText3d) placeholderText3d.style.display = 'block';
                        if(placeholderText3d) placeholderText3d.textContent = "Fehler beim Laden der 3D-Ansicht: " + error.message;
                    } finally {
                        showLoading(false);
                    }
                    showLoading(false);
                    resolve();
                } else {
                    attempts++;
                    if (attempts >= maxAttempts) {
                        console.error("[WaitNiivue] Niivue Bibliothek nach 20 Versuchen nicht gefunden. Breche ab.");
                        if(statusMessage) statusMessage.textContent = "Fehler: 3D-Viewer-Komponente konnte nicht geladen werden.";
                        showLoading(false);
                        currentIndex = numTotalSlicesForPrevStage;
                        currentWorkflowStage = 'finished';
                        updateUIForStage();
                        clearInterval(initInterval);
                        resolve();
                    }
                }
            };
            const initInterval = setInterval(checkAndInit, interval);
        });
    }

    function populateTemplateSelectionList() {
    if (!templateImageListDiv || !templateSliceFromInput || !templateSliceToInput || !templateAxisInfoSpan) return;
    templateImageListDiv.innerHTML = '';
    const axisForTemplate = sam2FixatedAxis || currentAxis;
    templateAxisInfoSpan.textContent = axisForTemplate; // Achse anzeigen

    const numSlicesOnAxis = isVolumetric() ? (sliceCounts[axisForTemplate] || 0) : dicomFilenames.length;

    if (numSlicesOnAxis === 0) {
        templateImageListDiv.innerHTML = '<em>Keine Slices für Vorlage auf dieser Achse.</em>';
        templateSliceFromInput.value = '';
        templateSliceToInput.value = '';
        templateSliceFromInput.max = 1;
        templateSliceToInput.max = 1;
        updateConfirmButtonText();
        return;
    }

    // Setze Min/Max für die Eingabefelder
    templateSliceFromInput.min = 1;
    templateSliceFromInput.max = numSlicesOnAxis;
    // templateSliceFromInput.value = 1; // Optional: Standardwert

    templateSliceToInput.min = 1;
    templateSliceToInput.max = numSlicesOnAxis;
    // templateSliceToInput.value = numSlicesOnAxis; // Optional: Standardwert

    const filesToUse = isVolumetric() ? Array.from({length: numSlicesOnAxis}, (_, i) => `${axisForTemplate} Slice ${i+1}`) : dicomFilenames;

    filesToUse.forEach((filename, index) => {
        const label = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.dataset.index = index; // 0-basierter Index
        checkbox.className = 'panel-chk';
        label.appendChild(checkbox);
        label.appendChild(document.createTextNode(` ${index + 1}: ${filename}`)); // 1-basierte Anzeige
        templateImageListDiv.appendChild(label);
    });
    updateConfirmButtonText();
    }

    function updateWeightInfoDisplay(data = null) {
        const weightInfoDiv = document.getElementById('weight-info');
        const volumeDisplay = document.getElementById('volume-display');
        const weightDisplay = document.getElementById('weight-display');
        const densityDisplay = document.getElementById('density-display');

        if (!weightInfoDiv || !volumeDisplay || !weightDisplay || !densityDisplay) {
            return;
        }

        // Prüfe jede Information einzeln und setze den Text oder einen Platzhalter
        if (data && data.volume_cm3 != null) {
            volumeDisplay.textContent = data.volume_cm3.toFixed(2);
        } else {
            volumeDisplay.textContent = '-';
            }

        if (data && data.weight_g != null) {
            weightDisplay.textContent = data.weight_g.toFixed(2);
            } else {
                weightDisplay.textContent = '-';
            }

        if (data && data.material_density != null) {
            densityDisplay.textContent = data.material_density;
        } else {
            densityDisplay.textContent = '-';
        }
        
        // Always ensure the panel is visible
        weightInfoDiv.classList.remove('hidden');
    }

    function updateConfirmButtonText() { 
        if (!btnConfirmTemplateApply || !templateImageListDiv) return; 
        const selectedCount = templateImageListDiv.querySelectorAll('.panel-chk:checked').length; 
        btnConfirmTemplateApply.textContent = `Vorlage auf ${selectedCount} Slices anwenden`; 
        btnConfirmTemplateApply.disabled = (selectedCount === 0 || savedAnnotationTemplate === null); }
    if(btnSaveTemplate) btnSaveTemplate.addEventListener('click', () => { if (currentWorkflowStage !== 'annotate_sam' || annotations.length === 0) { if(statusMessage) statusMessage.textContent = "Keine SAM-Annotationen zum Speichern als Vorlage."; return; } savedAnnotationTemplate = JSON.parse(JSON.stringify(annotations)); if(statusMessage) statusMessage.textContent = "Aktuelle Annotationen als Vorlage gespeichert."; if(btnApplyTemplate) btnApplyTemplate.disabled = false; if(btnApplyTemplateMulti) btnApplyTemplateMulti.disabled = false; });
    if(btnApplyTemplate) btnApplyTemplate.addEventListener('click', () => { 
        if (currentWorkflowStage !== 'annotate_sam' || !savedAnnotationTemplate) return; 
        // Sicherheitsabfrage entfernt
        annotations = JSON.parse(JSON.stringify(savedAnnotationTemplate)); 
        drawImageAndAnnotations(); 
        if(btnUndo) btnUndo.disabled = annotations.length === 0; 
        updateUIForStage(); 
        if(statusMessage) statusMessage.textContent = "Vorlage auf aktuellen Slice angewendet.";
    });
    if(btnApplyTemplateMulti) btnApplyTemplateMulti.addEventListener('click', () => { if (currentWorkflowStage !== 'annotate_sam' || !savedAnnotationTemplate) { if(statusMessage) statusMessage.textContent = "Zuerst Vorlage speichern."; return; } if(templateSelectionPanel) templateSelectionPanel.classList.toggle('hidden'); if(!templateSelectionPanel.classList.contains('hidden')) populateTemplateSelectionList(); });
    if(panelSelectAllLink) panelSelectAllLink.addEventListener('click', (e) => { e.preventDefault(); if(templateImageListDiv) templateImageListDiv.querySelectorAll('.panel-chk').forEach(chk => chk.checked = true); updateConfirmButtonText(); });
    if(panelDeselectAllLink) panelDeselectAllLink.addEventListener('click', (e) => { e.preventDefault(); if(templateImageListDiv) templateImageListDiv.querySelectorAll('.panel-chk').forEach(chk => chk.checked = false); updateConfirmButtonText(); });
    if(templateImageListDiv) templateImageListDiv.addEventListener('change', (e) => { if(e.target.classList.contains('panel-chk')) updateConfirmButtonText(); });
    // Neue Funktion zur Segmentierung mehrerer Slices
    async function segmentMultipleSlices(sliceIndices, axisToApply) {
        if (batchSegmentationActive) {
            alert("Eine Massensegmentierung läuft bereits.");
            return;
        }
        
        batchSegmentationActive = true;
        const totalToProcess = sliceIndices.length;
        showProgress(`Starte SAM-Segmentierung für ${totalToProcess} Slices...`); // NEU: Starte Ladebalken

        let erfolgreichSegmentiert = 0;
        let fehlerBeiSegmentierung = 0;

        for (let i = 0; i < totalToProcess; i++) {
            const sliceIdxToSegment = sliceIndices[i];
            updateProgress(i, totalToProcess); // NEU: Update des Ladebalkens

            const currentSliceAnnotations = allAnnotations[axisToApply]?.[sliceIdxToSegment];
            if (!currentSliceAnnotations || currentSliceAnnotations.length === 0) {
                console.warn(`Keine Annotationen für Slice ${sliceIdxToSegment} auf Achse ${axisToApply} gefunden, überspringe Segmentierung.`);
                if (!savedIndicesPerAxis[axisToApply]) savedIndicesPerAxis[axisToApply] = new Set();
                savedIndicesPerAxis[axisToApply].add(sliceIdxToSegment);
                savedMethodPerSlice[`${axisToApply}_${sliceIdxToSegment}`] = 'sam_empty_template_applied';
                continue;
            }

            const payload = {
                box: currentSliceAnnotations.find(a => a.type === 'box' || a.type === 'freehand')?.data || null,
                points: currentSliceAnnotations.filter(a => a.type === 'positive' || a.type === 'negative').map(p => ({ coords: p.data, label: p.type === 'positive' ? 1 : 0 })),
                wl: fixedWL,
                ww: fixedWW,
                axis: axisToApply,
                sliceIndex: sliceIdxToSegment,
                original_filename: baseVolumeFilename,
                dicom_slice_filename: (fileType === 'dicom_series' && dicomFilenames[sliceIdxToSegment]) ? dicomFilenames[sliceIdxToSegment] : null
            };

            try {
                const response = await fetch('/segment_sam', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const result = await response.json();
                if (!response.ok || result.error) throw new Error(result.error || `Serverfehler für Slice ${sliceIdxToSegment}`);
                if (result.mask_saved) {
                    erfolgreichSegmentiert++;
                    if (!savedIndicesPerAxis[axisToApply]) savedIndicesPerAxis[axisToApply] = new Set();
                    savedIndicesPerAxis[axisToApply].add(sliceIdxToSegment);
                    savedMethodPerSlice[`${axisToApply}_${sliceIdxToSegment}`] = 'sam_template_applied';
                } else {
                    fehlerBeiSegmentierung++;
                }
            } catch (error) {
                fehlerBeiSegmentierung++;
                console.error(`Fehler bei SAM-Segmentierung für Slice ${sliceIdxToSegment}:`, error);
            }
        }

        updateProgress(totalToProcess, totalToProcess); // Setze auf 100% am Ende
        batchSegmentationActive = false;
        
        let abschlussNachricht = `Massen-SAM-Segmentierung abgeschlossen. ${erfolgreichSegmentiert} Slices erfolgreich, ${fehlerBeiSegmentierung} mit Fehlern.`;
        if(statusMessage) statusMessage.textContent = abschlussNachricht;
        console.log('[BATCH SAM]', abschlussNachricht);
        
        hideProgress(); // NEU: Blende den Ladebalken am Ende aus
        updateProgressDisplay(); // Haupt-Fortschrittsanzeige aktualisieren
        if(fileType === 'dicom_series') highlightActiveListItem();
        updateUIForStage();
        
        return erfolgreichSegmentiert;
    }

    if (btnConfirmTemplateApply) {
    btnConfirmTemplateApply.addEventListener('click', async () => { // Funktion wird async
        if (!savedAnnotationTemplate || !templateImageListDiv) return;

        const checkedIndices = Array.from(templateImageListDiv.querySelectorAll('.panel-chk:checked'))
                                  .map(chk => parseInt(chk.dataset.index, 10));

        if (checkedIndices.length === 0) {
            if (statusMessage) statusMessage.textContent = "Keine Slices für Vorlage ausgewählt.";
            return;
        }

        if (await showConfirmationModal("Vorlage anwenden?", `Soll die Vorlage auf ${checkedIndices.length} Slices angewendet werden? Dies überschreibt bestehende Annotationen auf diesen Slices.`)) {
            const axisToApply = sam2FixatedAxis || currentAxis;
            if (!allAnnotations[axisToApply]) allAnnotations[axisToApply] = {};

            checkedIndices.forEach(idx => {
                allAnnotations[axisToApply][idx] = JSON.parse(JSON.stringify(savedAnnotationTemplate));
            });

            if (statusMessage) statusMessage.textContent = `Vorlage auf ${checkedIndices.length} Slices der Achse '${axisToApply}' angewendet.`;
            if (templateSelectionPanel) templateSelectionPanel.classList.add('hidden');

            if (checkedIndices.includes(currentIndex) && axisToApply === currentAxis) {
                loadSamAnnotationsForIndex(currentAxis, currentIndex);
                drawImageAndAnnotations();
            }

            // --- NEUE ABFRAGE UND LOGIK FÜR DIE DIREKTE SEGMENTIERUNG ---
            if (await showConfirmationModal("Direkt segmentieren?", `Soll für diese ${checkedIndices.length} Slices auch direkt die SAM-Segmentierung durchgeführt werden? Dies kann einige Zeit dauern.`)) {
                const erfolgreichSegmentiert = await segmentMultipleSlices(checkedIndices, axisToApply);
                
                // Nach der Batch-Segmentierung zur Finalisierung überleiten
                if (erfolgreichSegmentiert > 0) {
                    if (await showConfirmationModal("Batch-Segmentierung abgeschlossen", "Möchten Sie jetzt die 3D-Maske erstellen?")) {
                        await triggerFinishSequence('sam', axisToApply);
                    }
                }
            }
        }
    });
}
    if(btnMarkOthersEmptySam) btnMarkOthersEmptySam.addEventListener('click', async () => {
        if (currentWorkflowStage !== 'annotate_sam') return; 
        
        const axisToProcess = sam2FixatedAxis || currentAxis; 
        const totalForAxis = isVolumetric() ? (sliceCounts[axisToProcess] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0); 
        
        if (!savedIndicesPerAxis[axisToProcess]) savedIndicesPerAxis[axisToProcess] = new Set(); 
        const unsavedCount = totalForAxis - savedIndicesPerAxis[axisToProcess].size; 
        
        if (unsavedCount <= 0 && totalForAxis > 0) { 
            if(statusMessage) statusMessage.textContent = "Alle Slices dieser Achse bereits bearbeitet."; 
            return; 
        }
        
        if (await showConfirmationModal("Restliche Slices überspringen?", `Wollen Sie wirklich die ${unsavedCount > 0 ? unsavedCount : 'übrigen'} Slices der Achse '${axisToProcess}' als 'ohne Segmentierung (SAM)' markieren und abschließen?`)) { 
            try {
                if(statusMessage) statusMessage.textContent = "Markiere restliche Slices als leer..."; 
                showLoading(true); 
                
                for (let i = 0; i < totalForAxis; i++) { 
                    if (!savedIndicesPerAxis[axisToProcess].has(i)) { 
                        savedIndicesPerAxis[axisToProcess].add(i); 
                        savedMethodPerSlice[`${axisToProcess}_${i}`] = 'sam_empty'; 
                        if(allAnnotations[axisToProcess] && allAnnotations[axisToProcess][i]) delete allAnnotations[axisToProcess][i]; 
                    } 
                } 
                
                updateProgressDisplay(); 
                if(fileType === 'dicom_series') highlightActiveListItem(); 
                
                await triggerFinishSequence('sam', axisToProcess); 
            } catch (error) {
                console.error("Fehler beim Abschließen:", error);
                if(statusMessage) statusMessage.textContent = `Fehler beim Abschließen: ${error.message}`;
            } finally {
                showLoading(false); 
            }
        }
    });
    if(cbRemoveOutliersSam && inputMinOutlierSizeSam) { cbRemoveOutliersSam.addEventListener('change', () => { inputMinOutlierSizeSam.disabled = !cbRemoveOutliersSam.checked; }); inputMinOutlierSizeSam.disabled = !cbRemoveOutliersSam.checked; } // Initialen Zustand setzen
    if(cbRemoveOutliersThresh && inputMinOutlierSizeThresh) { cbRemoveOutliersThresh.addEventListener('change', () => { inputMinOutlierSizeThresh.disabled = !cbRemoveOutliersThresh.checked; }); inputMinOutlierSizeThresh.disabled = !cbRemoveOutliersThresh.checked; } // Initialen Zustand setzen

    document.addEventListener('keydown', (event) => {
        if (['INPUT', 'TEXTAREA'].includes(event.target.tagName) && !['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) { return; }
        if (loadingSpinner && loadingSpinner.style.display !== 'none') return; if (currentWorkflowStage === 'finished') return;
        const navAxis = isVolumetric() ? (sam2FixatedAxis || currentAxis) : 'axial';
        const totalForNav = isVolumetric() ? (sliceCounts[navAxis] || 0) : (fileType === 'dicom_series' ? dicomFilenames.length : 0);
        switch (event.key) {
            case 'ArrowUp': case 'ArrowLeft': if (currentIndex > 0) { event.preventDefault(); loadImage(currentIndex - 1, navAxis); } break;
            case 'ArrowDown': case 'ArrowRight':
                event.preventDefault();
                if (currentWorkflowStage === 'annotate_sam' && btnNextSam && !btnNextSam.disabled) {
                    // Nur noch den Button auslösen, der jetzt keine automatische Markierung mehr macht
                    btnNextSam.click(); // Ruft Navigation zum nächsten Slice
                } else if (currentIndex < totalForNav - 1) {
                    loadImage(currentIndex + 1, navAxis);
                } else if (currentIndex === totalForNav - 1 && totalForNav > 0) { // Letzter Slice, aber kein SAM save
                    loadImage(currentIndex + 1, navAxis); // Löst Finalisierungslogik in loadImage aus
                }
                break;
            case 'z': if ((event.ctrlKey || event.metaKey) && currentWorkflowStage === 'annotate_sam' && btnUndo && !btnUndo.disabled) { event.preventDefault(); btnUndo.click(); } break;
        }
    });

    // Event-Listener für Fenstergrößenänderung
    let resizeTimeout = null;
    window.addEventListener('resize', () => {
        // Verzögerte Ausführung, um zu häufige Aufrufe zu vermeiden
        if (resizeTimeout) clearTimeout(resizeTimeout);
        resizeTimeout = setTimeout(() => {
            console.log("Window resize: Passe Canvas-Größe an");
            resizeCanvasToFitContainer();
            drawImageAndAnnotations();
            if (currentWorkflowStage === 'annotate_thresh' && currentRawPixelData) {
                updateThresholdPreview();
            }
        }, 200);
    });

    // --- HYBRID-ANSATZ FUNKTIONEN (SAM + Thresholding) ---
    
    function showHybridRefinementButton() {
        if (btnRefineWithThreshold) {
            btnRefineWithThreshold.classList.remove('hidden');
        }
    }
    
    function hideHybridRefinementButton() {
        if (btnRefineWithThreshold) {
            btnRefineWithThreshold.classList.add('hidden');
        }
        if (hybridRefinementContainer) {
            hybridRefinementContainer.classList.add('hidden');
        }
        refinementMode = false;
    }
    
    function setupHybridEventListeners() {
        // SAM-Verfeinerungs-Button
        if (btnRefineWithThreshold) {
            btnRefineWithThreshold.addEventListener('click', async () => {
                if (!currentSamMask) {
                    alert('Keine SAM-Maske zum Verfeinern verfügbar. Führen Sie zuerst eine SAM-Segmentierung durch.');
                    return;
                }
                enterHybridRefinementMode();
            });
        }
        
        // Hybrid-Threshold-Slider synchronisieren
        if (hybridSliderThreshLower && hybridInputThreshLower && hybridDisplayThreshLower) {
            const syncHybridLower = () => {
                const value = parseInt(hybridSliderThreshLower.value);
                hybridInputThreshLower.value = value;
                hybridDisplayThreshLower.textContent = value;
                hybridThresholdLower = value;
                if (refinementMode) {
                    updateHybridPreview();
                }
            };
            
            hybridSliderThreshLower.addEventListener('input', syncHybridLower);
            hybridInputThreshLower.addEventListener('input', () => {
                const value = parseInt(hybridInputThreshLower.value) || -500;
                hybridSliderThreshLower.value = value;
                syncHybridLower();
            });
            
            // Stepper-Buttons für Lower Threshold
            const hybridStepButtons = document.querySelectorAll('.step-btn[data-target="hybrid-input-thresh-lower"]');
            hybridStepButtons.forEach(btn => {
                btn.addEventListener('click', () => {
                    const isUp = btn.classList.contains('step-up');
                    const step = isUp ? 5 : -5;
                    const currentValue = parseInt(hybridSliderThreshLower.value);
                    const newValue = isUp 
                        ? Math.min(currentValue + step, parseInt(hybridSliderThreshLower.max))
                        : Math.max(currentValue + step, parseInt(hybridSliderThreshLower.min));
                    hybridSliderThreshLower.value = newValue;
                    syncHybridLower();
                    console.log('[HYBRID] Lower Threshold Stepper:', isUp ? 'up' : 'down', 'zu', newValue);
                });
            });
        }
        
        if (hybridSliderThreshUpper && hybridInputThreshUpper && hybridDisplayThreshUpper) {
            const syncHybridUpper = () => {
                const value = parseInt(hybridSliderThreshUpper.value);
                hybridInputThreshUpper.value = value;
                hybridDisplayThreshUpper.textContent = value;
                hybridThresholdUpper = value;
                if (refinementMode) {
                    updateHybridPreview();
                }
            };
            
            hybridSliderThreshUpper.addEventListener('input', syncHybridUpper);
            hybridInputThreshUpper.addEventListener('input', () => {
                const value = parseInt(hybridInputThreshUpper.value) || 500;
                hybridSliderThreshUpper.value = value;
                syncHybridUpper();
            });
            
            // Stepper-Buttons für Upper Threshold
            const hybridStepButtonsUpper = document.querySelectorAll('.step-btn[data-target="hybrid-input-thresh-upper"]');
            hybridStepButtonsUpper.forEach(btn => {
                btn.addEventListener('click', () => {
                    const isUp = btn.classList.contains('step-up');
                    const step = isUp ? 5 : -5;
                    const currentValue = parseInt(hybridSliderThreshUpper.value);
                    const newValue = isUp 
                        ? Math.min(currentValue + step, parseInt(hybridSliderThreshUpper.max))
                        : Math.max(currentValue + step, parseInt(hybridSliderThreshUpper.min));
                    hybridSliderThreshUpper.value = newValue;
                    syncHybridUpper();
                    console.log('[HYBRID] Upper Threshold Stepper:', isUp ? 'up' : 'down', 'zu', newValue);
                });
            });
        }
        
        // Hybrid-Refinement Buttons
        if (btnApplyHybridRefinement) {
            btnApplyHybridRefinement.addEventListener('click', applyHybridRefinement);
        }
        
        if (btnCancelHybridRefinement) {
            btnCancelHybridRefinement.addEventListener('click', cancelHybridRefinement);
        }
        

        
        // Few-Shot Threshold Checkbox Event-Listener
        const cbFewShotThresholdEnabled = document.getElementById('cb-fewshot-threshold-enabled');
        if (cbFewShotThresholdEnabled) {
            cbFewShotThresholdEnabled.addEventListener('change', () => {
                fewShotThresholdEnabled = cbFewShotThresholdEnabled.checked;
                console.log('[HYBRID] Few-Shot Threshold-Verfeinerung:', fewShotThresholdEnabled ? 'aktiviert' : 'deaktiviert');
                if (fewShotThresholdEnabled) {
                    console.log('[HYBRID] Verwendet Threshold-Werte:', hybridThresholdLower, 'bis', hybridThresholdUpper);
                }
            });
        }
    }
    
    function enterHybridRefinementMode() {
        refinementMode = true;
        
        if (hybridRefinementContainer) {
            hybridRefinementContainer.classList.remove('hidden');
        }
        if (btnRefineWithThreshold) {
            btnRefineWithThreshold.classList.add('hidden');
        }
        
        // WICHTIG: Verstecke alle anderen Overlays für bessere Sichtbarkeit
        // Das SAM-Overlay wird ausgeblendet, während Threshold-Modus aktiv ist
        clearPreviewCanvas();
        
        // Threshold-Werte auf Raw-Data-Range anpassen
        if (currentRawPixelData) {
            updateHybridThresholdRange(currentRawPixelData);
        }
        
        // Erste Preview anzeigen
        updateHybridPreview();
        
        // Automatisch zur Original-View wechseln für bessere Sichtbarkeit
        switchToView('original');
        
        console.log('[HYBRID] Refinement-Modus aktiviert - SAM-Overlay ausgeblendet, zur Original-View gewechselt');
    }
    
    function cancelHybridRefinement() {
        refinementMode = false;
        
        if (hybridRefinementContainer) {
            hybridRefinementContainer.classList.add('hidden');
        }
        if (btnRefineWithThreshold) {
            btnRefineWithThreshold.classList.remove('hidden');
        }
        
        // Preview-Canvas löschen und zurück zur ursprünglichen SAM-Maske
        clearPreviewCanvas();
        drawImageAndAnnotations();
        
        // Zurück zur Segmentierungs-View wechseln
        switchToView('segmentation');
        
        console.log('[HYBRID] Refinement-Modus beendet, zurück zur Segmentierungs-View');
    }
    
    function updateHybridThresholdRange(rawDataArray) {
        // Regler-Bereich: bevorzugt der GLOBALE HU-Bereich des gesamten Volumens,
        // damit auch helle Objekte auf anderen Slices (höhere HU) erreichbar sind
        // und du dir die optimalen Werte aussuchen kannst. Fallback: Slice-Bereich.
        let minVal, maxVal;
        if (globalDataMin !== null && globalDataMax !== null && globalDataMax > globalDataMin) {
            minVal = globalDataMin;
            maxVal = globalDataMax;
        } else if (rawDataArray && rawDataArray.length) {
            minVal = Infinity; maxVal = -Infinity;
            for (let i = 0; i < rawDataArray.length; i++) {
                const val = rawDataArray[i];
                if (val < minVal) minVal = val;
                if (val > maxVal) maxVal = val;
            }
        } else {
            return;
        }

        if (minVal === Infinity || maxVal === -Infinity || minVal >= maxVal) {
            minVal = -1000; maxVal = 1000;
        }

        // Kleine Reserve, damit die Extremwerte gut erreichbar sind
        const range = maxVal - minVal;
        const extendedMin = Math.floor(minVal - range * 0.02);
        const extendedMax = Math.ceil(maxVal + range * 0.02);

        // Regler- und Eingabe-Bereiche setzen
        if (hybridSliderThreshLower && hybridInputThreshLower) {
            hybridSliderThreshLower.min = extendedMin;
            hybridSliderThreshLower.max = extendedMax;
            hybridInputThreshLower.min = extendedMin;
            hybridInputThreshLower.max = extendedMax;
        }

        if (hybridSliderThreshUpper && hybridInputThreshUpper) {
            hybridSliderThreshUpper.min = extendedMin;
            hybridSliderThreshUpper.max = extendedMax;
            hybridInputThreshUpper.min = extendedMin;
            hybridInputThreshUpper.max = extendedMax;
        }

        const src = (globalDataMin !== null && globalDataMax !== null) ? 'global' : 'slice';
        console.log(`[HYBRID] Threshold-Regler-Bereich (${src}): [${extendedMin}, ${extendedMax}]`);
    }
    
    function updateHybridPreview() {
        if (!refinementMode || !currentSamMask || !currentRawPixelData) return;
        
        // Lösche zuerst ALLE Overlays um Stapeln zu verhindern
        clearPreviewCanvas();
        
        // Erstelle verfeinerte Maske durch Kombination von SAM-Maske und Thresholding
        const width = originalWidth;
        const height = originalHeight;
        const refinedMask = new Uint8Array(width * height);
        
        for (let i = 0; i < currentRawPixelData.length; i++) {
            const samValue = currentSamMask[i];
            const rawValue = currentRawPixelData[i];
            
            // Nur dort verfeinern, wo SAM-Maske aktiv ist
            if (samValue > 0) {
                if (rawValue >= hybridThresholdLower && rawValue <= hybridThresholdUpper) {
                    refinedMask[i] = 1;
                }
            }
        }
        
        // Zeichne NUR das Hybrid-Overlay (kein SAM-Overlay mehr sichtbar)
        drawHybridRefinementOverlay(refinedMask, width, height);
    }
    
    function drawHybridRefinementOverlay(refinedMask, width, height) {
        if (!previewCtx || !previewCanvas) return;
        
        console.log('[HYBRID] Drawing overlay with zoom:', scale, 'origin:', originX, originY);
        
        // Lösche zuerst das Preview-Canvas komplett
        previewCtx.save();
        previewCtx.setTransform(1, 0, 0, 1, 0, 0);
        previewCtx.clearRect(0, 0, previewCanvas.width, previewCanvas.height);
        previewCtx.restore();
        
        // Erstelle temporäres Canvas für die Maske
        const tempCanvas = document.createElement('canvas');
        tempCanvas.width = width;
        tempCanvas.height = height;
        const tempCtx = tempCanvas.getContext('2d');
        
        // Erstelle ImageData für verfeinerte Maske
        const imageData = tempCtx.createImageData(width, height);
        const data = imageData.data;
        
        for (let i = 0; i < refinedMask.length; i++) {
            const maskValue = refinedMask[i];
            const pixelIndex = i * 4;
            
            if (maskValue > 0) {
                // Verfeinerte Bereiche in Lila anzeigen (bessere Sichtbarkeit bei hellen Objekten)
                data[pixelIndex] = 128;   // R
                data[pixelIndex + 1] = 0;   // G
                data[pixelIndex + 2] = 255; // B (Lila)
                data[pixelIndex + 3] = 140; // A (gute Sichtbarkeit)
            } else {
                data[pixelIndex + 3] = 0; // Vollständig transparent
            }
        }
        
        tempCtx.putImageData(imageData, 0, 0);
        
        // Zeichne auf Preview-Canvas mit korrekter Zoom-Transformation
        previewCtx.save();
        previewCtx.setTransform(scale, 0, 0, scale, originX, originY);
        previewCtx.drawImage(tempCanvas, 0, 0);
        previewCtx.restore();
        
        console.log('[HYBRID] Overlay gezeichnet');
    }
    
    async function applyHybridRefinement() {
    console.log('[HYBRID] Starte Speicherung der Verfeinerungs-Parameter...');
    try {
        const response = await fetch('/refine_sam_with_threshold', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                threshold_lower: hybridThresholdLower,
                threshold_upper: hybridThresholdUpper,
                ww: currentWW,
                wl: currentWL,
                axis: sam2FixatedAxis || currentAxis
            })
        });
        const result = await response.json();

        if (result.success) {
            cancelHybridRefinement(); 

            await showSimpleAlert("Parameter Gespeichert", result.message);
            if (statusMessage) {
                statusMessage.textContent = "Hybrid-Parameter für Few-Shot gespeichert.";
            }

        } else {
            await showSimpleAlert("Fehler", `Fehler beim Speichern der Parameter: ${result.error}`);
        }

    } catch (error) {
        console.error('Fehler bei der Speicherung der Hybrid-Parameter:', error);
        await showSimpleAlert("Netzwerkfehler", `Netzwerkfehler beim Speichern der Parameter: ${error.message}`);
    } finally {
        hideBlockingLoader();
    }
}

    function initializeApp() {
        console.log("Element-Check:", { previewAreaContainer, preview3dArea });
        console.log("initializeApp: Starte Initialisierung...");
        showLoading(true);
        if(statusMessage) statusMessage.textContent = "Initialisiere Anwendung...";

        let rawInitialFileType = (typeof initialFileType !== 'undefined' && initialFileType !== null) ? initialFileType : 'dicom_series';
        if (typeof rawInitialFileType === 'string' && rawInitialFileType.length >= 2 && rawInitialFileType.startsWith('"') && rawInitialFileType.endsWith('"')) {
            fileType = rawInitialFileType.substring(1, rawInitialFileType.length - 1);
        } else {
            fileType = rawInitialFileType;
        }
        console.log("[JS DEBUG] initialFileType aus HTML war:", `"${rawInitialFileType}"` , "; globales fileType ist jetzt:", `"${fileType}"`);


        console.log("[JS DEBUG] Roher String für initialSliceCounts VOR Parse:", initialSliceCounts);
        console.log("[JS DEBUG] Typ von initialSliceCounts VOR Parse:", typeof initialSliceCounts);

        try {
            sliceCounts = (typeof initialSliceCounts !== 'undefined' && initialSliceCounts !== null) ? JSON.parse(initialSliceCounts) : {'axial':0,'sagittal':0,'coronal':0};
            console.log("[JS DEBUG] sliceCounts NACH JSON.parse (im try):", JSON.parse(JSON.stringify(sliceCounts)));
            console.log("[JS DEBUG] Typ von sliceCounts NACH JSON.parse (im try):", typeof sliceCounts);
        }
        catch(e) {
            console.error("Fehler Parsen initialSliceCounts:", e, "Raw:", initialSliceCounts);
            sliceCounts = {'axial':0,'sagittal':0,'coronal':0};
            console.log("[JS DEBUG] sliceCounts im CATCH Block gesetzt auf:", JSON.parse(JSON.stringify(sliceCounts)));
        }

        let parsedImageFiles = [];
        try {
            if (typeof initialImageFiles !== 'undefined' && initialImageFiles !== null) {
                parsedImageFiles = JSON.parse(initialImageFiles);
            }
        } catch (e) {
            console.error("Fehler Parsen initialImageFiles:", e, "Raw:", initialImageFiles);
        }

        if (fileType === 'dicom_series') {
            dicomFilenames = Array.isArray(parsedImageFiles) ? parsedImageFiles : [];
        } else if (isVolumetric()) {
            try {
                baseVolumeFilename = (typeof initialBaseFilename !== 'undefined' && initialBaseFilename !== null) ? JSON.parse(initialBaseFilename) : "volume_data";
            } catch(e) {
                console.error("Fehler Parsen initialBaseFilename:", e);
                baseVolumeFilename = "volume_data";
            }
            dicomFilenames = [];
        }

        console.log("App Init - FileType:", `"${fileType}"`, "SliceCounts:", JSON.parse(JSON.stringify(sliceCounts)), "DICOMs:", dicomFilenames.length, "BaseVolume:", baseVolumeFilename);

        currentAxis = 'axial';
        console.log("[JS DEBUG] Check vor Zuweisung zu totalSlices: fileType =", `"${fileType}"`, "; isVolumetric() =", isVolumetric());

        if (fileType === 'dicom_series') {
            totalSlicesForCurrentAxis = dicomFilenames.length;
        } else if (isVolumetric()) {
            console.log("---- START DEBUG BLOCK FÜR totalSlicesForCurrentAxis ----");
            console.log("[1] currentAxis Wert:", currentAxis);
            console.log("[2] currentAxis Typ:", typeof currentAxis);
            console.log("[3] sliceCounts Objekt:", JSON.parse(JSON.stringify(sliceCounts)));
            console.log("[4] sliceCounts Typ:", typeof sliceCounts);
            let keyBeingAccessed = currentAxis;
            console.log("[5] Schlüssel für Zugriff (keyBeingAccessed):", keyBeingAccessed);
            let valueExtracted = sliceCounts[keyBeingAccessed]; // sliceCounts.axial
            console.log("[6] Wert extrahiert (valueExtracted = sliceCounts[keyBeingAccessed]):", valueExtracted);
            console.log("[7] Typ des extrahierten Werts (typeof valueExtracted):", typeof valueExtracted);
            let valueForParseInt = valueExtracted;
            let intValue = parseInt(valueForParseInt, 10);
            console.log("[8] Wert an parseInt übergeben (valueForParseInt):", valueForParseInt);
            console.log("[9] Ergebnis von parseInt (intValue):", intValue);
            console.log("[10] Typ des Ergebnisses von parseInt (typeof intValue):", typeof intValue);
            let finalValueForTotalSlices = intValue || 0;
            console.log("[11] Wert nach '|| 0' (finalValueForTotalSlices):", finalValueForTotalSlices);
            totalSlicesForCurrentAxis = finalValueForTotalSlices;
            console.log("[12] totalSlicesForCurrentAxis nach Zuweisung (Variable der Funktion):", totalSlicesForCurrentAxis);
            console.log("---- END DEBUG BLOCK FÜR totalSlicesForCurrentAxis ----");
        } else {
            totalSlicesForCurrentAxis = 0;
        }

        console.log("initializeApp: Initial totalSlicesForCurrentAxis =", totalSlicesForCurrentAxis, "für Achse", currentAxis);

        axisButtons.forEach(btn => { if(btn) btn.classList.toggle('active', btn.dataset.axis === currentAxis)});

        populateImageList(dicomFilenames);
        updateSliceSlider();
        updateWLWWControls();
        updateThresholdSliderMinMax([]);
        updateUIForStage();
        
        const canLoad = (fileType === 'dicom_series' && dicomFilenames.length > 0) || (isVolumetric() && totalSlicesForCurrentAxis > 0);
        console.log("initializeApp: canLoad =", canLoad);

        if (canLoad) {
            if(statusMessage) statusMessage.textContent = "Lade erstes Bild...";
            loadImage(0, currentAxis);
        } else {
            if (statusMessage) statusMessage.textContent = "Keine Slices zum Laden.";
            console.log("initializeApp: Keine ladbaren Slices.");
            showLoading(false);
        }
        updateWeightInfoDisplay();
    }

    // Beim Laden prüfen, ob ein absurd anisotropes Voxel-Spacing vorliegt (Header-Fehler) und
    // ggf. anbieten, es zu korrigieren – im 2D-Viewer unsichtbar, streckt aber das 3D-Modell.
    async function checkSpacingAnomaly() {
        try {
            const resp = await fetch('/get_current_config');
            const cfg = await resp.json();
            const anom = cfg && cfg.spacing_anomaly;
            if (!anom) return;
            const ok = await showConfirmationModal(
                "Ungewöhnliches Voxel-Spacing erkannt",
                `${anom.message} Jetzt auf ${anom.suggested} mm korrigieren? (empfohlen)`);
            if (!ok) return;
            const r = await fetch('/fix_spacing', { method: 'POST' });
            const res = await r.json();
            if (res.success) {
                if (statusMessage) statusMessage.textContent =
                    `Voxel-Spacing korrigiert (Achse ${res.axis}: ${res.from} → ${res.to} mm).`;
            } else {
                alert("Spacing-Korrektur fehlgeschlagen: " + (res.error || "unbekannt"));
            }
        } catch (e) {
            console.warn("Spacing-Check übersprungen:", e);
        }
    }

    if (!canvas || !previewCanvas || !sliceSlider || !axisSelectorContainer ) {
        console.error("Kritische DOM-Elemente fehlen! App kann nicht vollständig initialisiert werden.");
        if(statusMessage) statusMessage.textContent = "Fehler: UI-Elemente fehlen.";
        return;
    }

    initializeApp();
    checkSpacingAnomaly();
    attachSliceRangeHandlers();

            // Generiert Support-Masken automatisch aus den Annotationen einer geladenen JSON-Datei.
    // Spiegelt exakt den manuellen Ablauf beim Segment-Button-Klick.
    async function regenerateSupportMasksFromAnnotations(annotationsBySlice, axis) {
        const sliceKeys = Object.keys(annotationsBySlice);
        if (sliceKeys.length === 0) return;

        let successCount = 0;
        const errors = [];

        for (let i = 0; i < sliceKeys.length; i++) {
            const sliceIndex = parseInt(sliceKeys[i]);
            const sliceAnnotations = annotationsBySlice[sliceKeys[i]];

            showBlockingLoader(`Generiere Support-Maske ${i + 1} von ${sliceKeys.length} (Slice ${sliceIndex})...`);

            console.log(`[JSON-Restore] Slice ${sliceIndex} raw annotations:`, JSON.stringify(sliceAnnotations));
            const points = sliceAnnotations
                .filter(a => a.type === 'positive' || a.type === 'negative')
                .map(a => ({ coords: a.data || a.coordinates, label: a.type === 'positive' ? 1 : 0 }));

            const boxAnnotation = sliceAnnotations.find(a => a.type === 'box');
            const box = boxAnnotation ? (boxAnnotation.data || boxAnnotation.coordinates) : null;

            console.log(`[JSON-Restore] Slice ${sliceIndex} extracted points:`, JSON.stringify(points), 'box:', JSON.stringify(box));

            if (points.length === 0 && !box) {
                console.warn(`Slice ${sliceIndex}: keine Punkte und keine Box, übersprungen.`);
                continue;
            }

            const payload = {
                box: box,
                points: points,
                wl: fixedWL,
                ww: fixedWW,
                axis: axis,
                sliceIndex: sliceIndex,
                original_filename: baseVolumeFilename,
                dicom_slice_filename: (fileType === 'dicom_series' && dicomFilenames[sliceIndex]) ? dicomFilenames[sliceIndex] : null
            };

            console.log(`[JSON-Restore] Segment-Aufruf Slice ${sliceIndex} (${axis}):`, payload);

            try {
                const response = await fetch('/segment_sam', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const result = await response.json();

                if (!response.ok || result.error) {
                    const msg = result.error || `HTTP ${response.status}`;
                    errors.push(`Slice ${sliceIndex}: ${msg}`);
                    console.error(`[JSON-Restore] Slice ${sliceIndex} Fehler:`, msg);
                    continue;
                }

                if (result.mask_saved) {
                    if (!savedIndicesPerAxis[axis]) savedIndicesPerAxis[axis] = new Set();
                    savedIndicesPerAxis[axis].add(sliceIndex);
                    savedMethodPerSlice[`${axis}_${sliceIndex}`] = 'sam';
                    // overlay_url für diesen Slice merken (wird von loadImage überschrieben beim Navigieren)
                    currentSegmentationUrl = result.overlay_url;
                    successCount++;
                    console.log(`[JSON-Restore] Slice ${sliceIndex} OK. area=${result.area} overlay_url=${result.overlay_url}`);
                } else {
                    const msg = result.message || 'mask_saved=false';
                    errors.push(`Slice ${sliceIndex}: ${msg}`);
                    console.warn(`[JSON-Restore] Slice ${sliceIndex} nicht gespeichert:`, msg);
                }
            } catch (err) {
                errors.push(`Slice ${sliceIndex}: ${err.message}`);
                console.error(`[JSON-Restore] Netzwerkfehler Slice ${sliceIndex}:`, err);
            }
        }

        hideBlockingLoader();

        if (errors.length > 0) {
            alert(`Fehler bei der Support-Masken-Generierung:\n${errors.join('\n')}`);
        }

        if (successCount > 0) {
            updateFewshotMaskList();
            updateProgressDisplay();
            if (statusMessage) statusMessage.textContent = `${successCount} von ${sliceKeys.length} Support-Maske(n) aus JSON wiederhergestellt.`;
        }
    }

        // NEUE FUNKTION ZUM ANWENDEN VON EINSTELLUNGEN AUS EINER JSON-DATEI
    async function applySettingsFromJson(settings, options = {}) {
        const applyWindowing = options.applyWindowing !== false;
        const applyMethod    = options.applyMethod    !== false;
        const applyThreshold = options.applyThreshold === true
            && settings.hybrid_refinement
            && settings.hybrid_refinement.threshold_lower != null
            && settings.hybrid_refinement.threshold_upper != null;
        try {
            console.log("Wende Einstellungen aus JSON an:", settings, "options:", options);

            // 1. Fensterung anwenden
            if (applyWindowing) {
                if (settings.parameters && settings.parameters.window_level != null && settings.parameters.window_width != null) {
                    fixedWL = parseFloat(settings.parameters.window_level);
                    fixedWW = parseFloat(settings.parameters.window_width);
                    currentWL = fixedWL;
                    currentWW = fixedWW;
                    updateWLWWControls();
                    console.log(`Fensterung gesetzt: WL=${fixedWL}, WW=${fixedWW}`);
                } else {
                    throw new Error("Fensterungs-Parameter fehlen in der JSON-Datei.");
                }
            }

            // Threshold-Verfeinerungs-Werte in UI übernehmen (unabhängig von Methode)
            if (applyThreshold) {
                hybridThresholdLower = parseFloat(settings.hybrid_refinement.threshold_lower);
                hybridThresholdUpper = parseFloat(settings.hybrid_refinement.threshold_upper);
                if (hybridSliderThreshLower) hybridSliderThreshLower.value = hybridThresholdLower;
                if (hybridInputThreshLower) hybridInputThreshLower.value = Math.round(hybridThresholdLower);
                if (hybridDisplayThreshLower) hybridDisplayThreshLower.textContent = Math.round(hybridThresholdLower);
                if (hybridSliderThreshUpper) hybridSliderThreshUpper.value = hybridThresholdUpper;
                if (hybridInputThreshUpper) hybridInputThreshUpper.value = Math.round(hybridThresholdUpper);
                if (hybridDisplayThreshUpper) hybridDisplayThreshUpper.textContent = Math.round(hybridThresholdUpper);
                fewShotThresholdEnabled = true;
                if (cbFewShotThresholdEnabled) cbFewShotThresholdEnabled.checked = true;
                console.log(`Threshold-Verfeinerung gesetzt: ${hybridThresholdLower} bis ${hybridThresholdUpper}`);
            }

            // Nur Fensterung/Threshold gewünscht → Bild neu laden und fertig
            if (!applyMethod) {
                if (applyWindowing) {
                    await loadImage(currentIndex, currentAxis);
                }
                if (statusMessage) {
                    const bits = [];
                    if (applyWindowing) bits.push(`Fensterung WL=${Math.round(fixedWL)}, WW=${Math.round(fixedWW)}`);
                    if (applyThreshold) bits.push(`Threshold ${Math.round(hybridThresholdLower)}–${Math.round(hybridThresholdUpper)}`);
                    statusMessage.textContent = `Aus JSON geladen: ${bits.join(' · ')}.`;
                }
                updateUIForStage();
                return;
            }

            // 2. Methode und zugehörige Parameter anwenden
            const method = settings.method;
            if (!method) throw new Error("Methode fehlt in der JSON-Datei.");

            currentWorkflowStage = 'method_select'; // Simuliere den Schritt zur Methodenauswahl

            if (method === 'sam') {
                segmentationMethod = 'sam';
                currentWorkflowStage = 'annotate_sam';

                // SAM-spezifische Parameter laden
                sam2FixatedAxis = settings.parameters.segmentation_axis;
                let normalizedAnnotations = {};
                if (settings.annotations && settings.annotations.annotations_by_slice) {
                    allAnnotations = {};
                    // Normalisierung: JSON-Format (coordinates) → internes Format (data)
                    for (const [sliceKey, sliceAnns] of Object.entries(settings.annotations.annotations_by_slice)) {
                        normalizedAnnotations[sliceKey] = sliceAnns.map(ann => ({
                            type: ann.type,
                            data: ann.coordinates || ann.data,
                        }));
                    }
                    allAnnotations[sam2FixatedAxis] = normalizedAnnotations;
                    console.log(`${Object.keys(normalizedAnnotations).length} Annotationen für Achse '${sam2FixatedAxis}' geladen (normalisiert).`);

                    // Support-Masken aus JSON-Annotationen automatisch neu generieren (await!)
                    await regenerateSupportMasksFromAnnotations(normalizedAnnotations, sam2FixatedAxis);

                    // Threshold-Verfeinerung direkt auf die neu erzeugten Support-Masken anwenden
                    if (applyThreshold) {
                        try {
                            const refineResp = await fetch('/refine_sam_with_threshold', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    threshold_lower: hybridThresholdLower,
                                    threshold_upper: hybridThresholdUpper,
                                    ww: currentWW,
                                    wl: currentWL,
                                    axis: sam2FixatedAxis
                                })
                            });
                            const refineResult = await refineResp.json();
                            if (!refineResp.ok || !refineResult.success) {
                                console.warn('Threshold-Verfeinerung nach Import fehlgeschlagen:', refineResult.error);
                            } else {
                                console.log('Threshold-Verfeinerung auf importierte Masken angewendet:', refineResult.message);
                            }
                        } catch (err) {
                            console.warn('Fehler bei Threshold-Verfeinerung nach JSON-Import:', err);
                        }
                    }
                }

                // Post-Processing
                if(cbFillHolesSam) cbFillHolesSam.checked = settings.parameters.fill_holes || false;
                    cbFillHolesSam.dispatchEvent(new Event('change'));
                if(cbRemoveOutliersSam) cbRemoveOutliersSam.checked = settings.parameters.remove_outliers || false;
                    cbRemoveOutliersSam.dispatchEvent(new Event('change'));
                if(inputMinOutlierSizeSam) inputMinOutlierSizeSam.value = settings.parameters.min_outlier_size || 100;
                    inputMinOutlierSizeSam.dispatchEvent(new Event('change'));
                if(inputMaterialDensitySam) inputMaterialDensitySam.value = settings.parameters.material_density || '';
                    inputMaterialDensitySam.dispatchEvent(new Event('change'));

                // Zur Achse der Annotationen wechseln und ersten annotierten Slice anzeigen
                const annotationKeys = Object.keys(normalizedAnnotations);
                if (annotationKeys.length > 0) {
                    const firstSliceKey = annotationKeys[0];
                    const firstSliceIndex = parseInt(firstSliceKey);
                    currentAxis = sam2FixatedAxis;
                    currentIndex = firstSliceIndex;
                    annotations = JSON.parse(JSON.stringify(normalizedAnnotations[firstSliceKey] || []));
                }
                await loadImage(currentIndex, currentAxis);
                updateUIForStage();
                // Segmentierungsansicht aktivieren falls Maske vorhanden
                if (currentSegmentationUrl) {
                    switchToView('segmentation');
                }
                return;

            } else if (method === 'threshold') {
                segmentationMethod = 'threshold';
                currentWorkflowStage = 'annotate_thresh';

                // Thresholding-spezifische Parameter laden
                currentLowerThresh = parseFloat(settings.parameters.lower_threshold);
                currentUpperThresh = parseFloat(settings.parameters.upper_threshold);

                // UI-Slider aktualisieren
                if(sliderThreshLower) sliderThreshLower.value = currentLowerThresh;
                if(inputThreshLower) inputThreshLower.value = Math.round(currentLowerThresh);
                if(sliderThreshUpper) sliderThreshUpper.value = currentUpperThresh;
                if(inputThreshUpper) inputThreshUpper.value = Math.round(currentUpperThresh);
                updateThresholdDisplays();

                // Post-Processing
                if(cbFillHolesSam) cbFillHolesSam.checked = settings.parameters.fill_holes || false;
                    cbFillHolesSam.dispatchEvent(new Event('change'));
                if(cbRemoveOutliersSam) cbRemoveOutliersSam.checked = settings.parameters.remove_outliers || false;
                    cbRemoveOutliersSam.dispatchEvent(new Event('change'));
                if(inputMinOutlierSizeSam) inputMinOutlierSizeSam.value = settings.parameters.min_outlier_size || 100;
                    inputMinOutlierSizeSam.dispatchEvent(new Event('change'));
                if(inputMaterialDensitySam) inputMaterialDensitySam.value = settings.parameters.material_density || '';
                    inputMaterialDensitySam.dispatchEvent(new Event('change'));

            } else {
                throw new Error(`Unbekannte Methode in JSON: ${method}`);
            }

            // 3. UI aktualisieren und Bild neu laden
            if (statusMessage) statusMessage.textContent = `Einstellungen aus JSON für Methode '${method}' geladen.`;

            // Lade Bild neu mit korrekter Fensterung und zeige die Annotationen an
            await loadImage(currentIndex, currentAxis);
            updateUIForStage();

        } catch (error) {
            console.error("Fehler beim Verarbeiten der JSON-Datei:", error);
            alert(`Fehler: Die JSON-Datei konnte nicht verarbeitet werden.\n${error.message}`);
            // Setze UI zurück, falls etwas schiefgeht
            currentWorkflowStage = 'wl_select';
            updateUIForStage();
        }
    }

    function showJsonImportModal(settings, filename) {
        return new Promise((resolve) => {
            const existing = document.getElementById('json-import-modal');
            if (existing) existing.remove();

            const hasWindowing = settings.parameters && settings.parameters.window_level != null && settings.parameters.window_width != null;
            const hasMethod = !!settings.method;
            const hasThreshold = !!(settings.hybrid_refinement
                && settings.hybrid_refinement.threshold_lower != null
                && settings.hybrid_refinement.threshold_upper != null);

            const wlText  = hasWindowing ? `WL: ${Math.round(settings.parameters.window_level)}, WW: ${Math.round(settings.parameters.window_width)}` : 'nicht verfügbar';
            let methodText = 'nicht verfügbar';
            if (settings.method === 'sam' && settings.annotations) {
                const n = Object.keys(settings.annotations.annotations_by_slice || {}).length;
                const ax = (settings.annotations.segmentation_axis || settings.parameters?.segmentation_axis || '?');
                methodText = `SAM – Achse: ${ax}, ${n} Slice(s) annotiert`;
            } else if (settings.method === 'threshold') {
                const lo = settings.parameters?.lower_threshold != null ? Math.round(settings.parameters.lower_threshold) : '?';
                const hi = settings.parameters?.upper_threshold != null ? Math.round(settings.parameters.upper_threshold) : '?';
                methodText = `Threshold – ${lo} bis ${hi} HU`;
            } else if (settings.method) {
                methodText = settings.method;
            }
            const threshText = hasThreshold
                ? `${Math.round(settings.hybrid_refinement.threshold_lower)} bis ${Math.round(settings.hybrid_refinement.threshold_upper)} HU`
                : 'nicht verfügbar';

            const modal = document.createElement('div');
            modal.id = 'json-import-modal';
            modal.className = 'modal-overlay';
            modal.innerHTML = `
                <div class="modal-content" style="max-width:420px;">
                    <div class="modal-header">
                        <h3>JSON laden</h3>
                        <button class="close-modal">&times;</button>
                    </div>
                    <div class="modal-body">
                        <p style="margin-bottom:14px;font-size:0.82em;color:#999;word-break:break-all;">${filename}</p>
                        <p style="margin-bottom:16px;font-weight:500;">Was soll übernommen werden?</p>
                        <label style="display:flex;align-items:flex-start;gap:10px;margin-bottom:14px;cursor:pointer;">
                            <input type="checkbox" id="json-cb-windowing" ${hasWindowing ? 'checked' : 'disabled'} style="margin-top:3px;flex-shrink:0;">
                            <div>
                                <div style="font-weight:500;${!hasWindowing ? 'color:#555;' : ''}">Fensterung</div>
                                <div style="font-size:0.82em;color:#aaa;">${wlText}</div>
                            </div>
                        </label>
                        <label style="display:flex;align-items:flex-start;gap:10px;margin-bottom:14px;cursor:pointer;">
                            <input type="checkbox" id="json-cb-method" ${hasMethod ? 'checked' : 'disabled'} style="margin-top:3px;flex-shrink:0;">
                            <div>
                                <div style="font-weight:500;${!hasMethod ? 'color:#555;' : ''}">Segmentiermethode &amp; Parameter</div>
                                <div style="font-size:0.82em;color:#aaa;">${methodText}</div>
                            </div>
                        </label>
                        <label style="display:flex;align-items:flex-start;gap:10px;margin-bottom:22px;cursor:pointer;">
                            <input type="checkbox" id="json-cb-threshold" ${hasThreshold ? 'checked' : 'disabled'} style="margin-top:3px;flex-shrink:0;">
                            <div>
                                <div style="font-weight:500;${!hasThreshold ? 'color:#555;' : ''}">Threshold-Verfeinerung</div>
                                <div style="font-size:0.82em;color:#aaa;">${threshText}</div>
                            </div>
                        </label>
                        <div style="display:flex;gap:8px;justify-content:flex-end;">
                            <button id="json-import-cancel" class="btn btn-secondary" style="padding:6px 14px;">Abbrechen</button>
                            <button id="json-import-confirm" class="btn btn-primary" style="padding:6px 14px;">Übernehmen</button>
                        </div>
                    </div>
                </div>`;
            document.body.appendChild(modal);

            const close = (result) => {
                modal.remove();
                resolve(result);
            };

            modal.querySelector('.close-modal').addEventListener('click', () => close(null));
            modal.addEventListener('click', (e) => { if (e.target === modal) close(null); });
            document.getElementById('json-import-cancel').addEventListener('click', () => close(null));
            document.getElementById('json-import-confirm').addEventListener('click', () => {
                const applyWindowing = document.getElementById('json-cb-windowing').checked;
                const applyMethod   = document.getElementById('json-cb-method').checked;
                const applyThreshold = document.getElementById('json-cb-threshold').checked;
                if (!applyWindowing && !applyMethod && !applyThreshold) { close(null); return; }
                close({ applyWindowing, applyMethod, applyThreshold });
            });
        });
    }

    // ================================================================
    // Abschluss der Segmentierung: Ergebnis speichern
    //
    // Drei Wege, je nachdem was moeglich ist: als ZIP herunterladen, dasselbe
    // ZIP in einen iRODS-Ordner legen, oder - wenn die Serie aus dcm4chee
    // importiert wurde - die Maske als DICOM-SEG zur Originalserie in die
    // Datenbank zurueckschreiben.
    //
    // Die letzten beiden laufen bewusst ueber den Browser zu den Endpunkten
    // des InterACT-GUI: nur dort existiert die Keycloak-Sitzung, unter der
    // Rechte geprueft und Zugriffe protokolliert werden. Dieser Container
    // kennt weder iRODS noch die Datenbank, und die API hinter /api-proxy/
    // hat selbst keine Anmeldung - das GUI ist die einzige Stelle, die den
    // Schreibvorgang einer Person zuordnen kann.
    //
    // Die GUI-Pfade duerfen weder als href/src/action im Markup stehen noch
    // mit einem Routennamen dieses Containers beginnen: der Reverse-Proxy des
    // GUI wuerde ihnen sonst /proxy/colleague/ voranstellen (siehe
    // docker-context-gui/app/routes/reverse_proxy.py).
    // ================================================================
    const GUI_IRODS_PICKER = '/irods/browse/?picker=1&embedded=1';
    const GUI_IRODS_UPLOAD = '/irods/upload';
    const GUI_API_PROXY = '/api-proxy';

    const GUI_ONLY_HINT = 'Nur innerhalb der InterACT-GUI verfügbar - dort ist die Anmeldung '
                        + 'vorhanden, unter der Ihre Zugriffsrechte geprüft und protokolliert werden.';

    function setExportStatus(message, kind) {
        const box = document.getElementById('export-status');
        if (!box) return;
        if (!message) {
            box.classList.add('hidden');
            box.textContent = '';
            return;
        }
        box.textContent = message;
        box.classList.remove('error', 'success');
        if (kind) box.classList.add(kind);
        box.classList.remove('hidden');
    }

    function configureExportOption(key, enabled, reason) {
        const btn = document.querySelector(`.export-option[data-export="${key}"]`);
        if (!btn) return;
        btn.disabled = !enabled;
        const reasonEl = btn.querySelector('.export-option-reason');
        if (reasonEl) {
            reasonEl.textContent = enabled ? '' : (reason || '');
            reasonEl.classList.toggle('hidden', enabled || !reason);
        }
    }

    function closeExportModal() {
        const overlay = document.getElementById('export-modal-overlay');
        if (overlay) overlay.classList.add('hidden');
    }

    // Antwort von /export_options der zuletzt geoeffneten Auswahl - die
    // Handler brauchen daraus UIDs und Beschriftungen fuer den SEG-Upload.
    let exportOptions = null;

    async function openExportModal() {
        const overlay = document.getElementById('export-modal-overlay');
        if (!overlay) return;

        showBlockingLoader("Ergebnisse werden geprüft...");
        try {
            const resp = await fetch('/export_options');
            exportOptions = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(exportOptions.error || resp.statusText);
            }
        } catch (err) {
            hideBlockingLoader();
            updateStatusMessage("Ergebnisse konnten nicht ermittelt werden: " + err.message);
            return;
        }
        hideBlockingLoader();

        const inGui = !!exportOptions.embedded_in_gui;
        const seg = exportOptions.seg || {};

        configureExportOption('download', true, null);
        configureExportOption('irods', inGui, GUI_ONLY_HINT);
        configureExportOption(
            'seg',
            inGui && !!seg.available,
            inGui ? seg.reason : GUI_ONLY_HINT
        );

        setExportStatus('', null);
        overlay.classList.remove('hidden');
    }

    // Holt eine der Ergebnisdateien dieses Containers als Blob, statt sie dem
    // Browser als Download zu geben: sie soll ja weiter an das GUI.
    async function fetchResultBlob(url, description) {
        const resp = await fetch(url, { credentials: 'same-origin' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.error || `${description} konnte nicht erstellt werden (HTTP ${resp.status}).`);
        }
        const blob = await resp.blob();
        if (!blob.size) throw new Error(`${description} ist leer.`);
        return blob;
    }

    // Fehler der GUI-Endpunkte lesbar machen. Eine abgelaufene Sitzung kommt
    // je nach Endpunkt als 401 oder als Weiterleitung auf die Anmeldeseite
    // zurueck - beides ist kein JSON und waere sonst ein kryptischer Parse-Fehler.
    async function guiJson(resp) {
        if (resp.status === 401 || resp.status === 403) {
            throw new Error('Kein Zugriff (HTTP ' + resp.status + '). Bitte im InterACT-GUI neu anmelden.');
        }
        const data = await resp.json().catch(() => null);
        if (data === null) {
            throw new Error('Unerwartete Antwort des GUI (HTTP ' + resp.status
                          + '). Möglicherweise ist Ihre Anmeldung abgelaufen.');
        }
        return data;
    }

    function pickIrodsFolder() {
        return new Promise((resolve) => {
            const overlay = document.getElementById('irods-picker-overlay');
            const frame = document.getElementById('irods-picker-frame');
            const closeBtn = document.getElementById('irods-picker-close');
            if (!overlay || !frame) { resolve(null); return; }

            const finish = (path) => {
                window.removeEventListener('message', onMessage);
                overlay.classList.add('hidden');
                frame.setAttribute('src', 'about:blank');
                closeBtn.onclick = null;
                overlay.onclick = null;
                resolve(path);
            };

            // Der iRODS-Browser des GUI meldet die Auswahl per postMessage.
            const onMessage = (e) => {
                if (e.origin !== window.location.origin) return;
                if (!e.data || e.data.type !== 'irods-folder-picked') return;
                finish(e.data.path || '');
            };

            window.addEventListener('message', onMessage);
            closeBtn.onclick = () => finish(null);
            overlay.onclick = (e) => { if (e.target === overlay) finish(null); };
            frame.setAttribute('src', GUI_IRODS_PICKER);
            overlay.classList.remove('hidden');
        });
    }

    // Wartet auf einen Hintergrund-Auftrag der API (dieselbe Statusabfrage,
    // die auch die Batch-Seite des GUI benutzt).
    async function waitForApiJob(jobId, timeoutMs = 10 * 60 * 1000) {
        const url = GUI_API_PROXY + '/job_status/' + encodeURIComponent(jobId);
        const deadline = Date.now() + timeoutMs;

        while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 2000));
            const job = await guiJson(await fetch(url, { credentials: 'same-origin' }));
            if (job.status === 'done') return job.message || 'Fertig.';
            if (job.status === 'failed') throw new Error(job.error || 'Der Auftrag ist fehlgeschlagen.');
            if (job.status === 'not_found') throw new Error(job.error || 'Der Auftrag ist nicht mehr bekannt.');
        }
        throw new Error('Zeitüberschreitung: Der Auftrag läuft noch. '
                      + 'Prüfen Sie später, ob die Serie in der Datenbank angekommen ist.');
    }

    async function exportToIrods() {
        const relPath = await pickIrodsFolder();
        if (relPath === null) {
            setExportStatus('Ordnerauswahl abgebrochen - es wurde nichts gespeichert.', null);
            return;
        }

        showBlockingLoader("Ergebnisse werden gepackt...");
        try {
            const blob = await fetchResultBlob('/download_latest_segmentation', 'Das ZIP-Archiv');

            showBlockingLoader("Archiv wird in iRODS geschrieben...");
            const fd = new FormData();
            fd.append('ajax', '1');
            fd.append('rel_path', relPath);
            fd.append('file', blob, exportOptions.download_filename || 'segmentation_results.zip');

            const result = await guiJson(await fetch(GUI_IRODS_UPLOAD, {
                method: 'POST', body: fd, credentials: 'same-origin'
            }));
            if (!result.ok) {
                throw new Error((result.failed || []).join('; ') || result.error || 'Unbekannter Fehler.');
            }

            const target = relPath ? relPath : 'Ihr iRODS-Startordner';
            setExportStatus(`Gespeichert in ${target}: ${(result.succeeded || []).join(', ')}`, 'success');
            updateStatusMessage("Segmentierungsergebnis in iRODS gespeichert.");
        } catch (err) {
            setExportStatus('Speichern in iRODS fehlgeschlagen: ' + err.message, 'error');
        } finally {
            hideBlockingLoader();
        }
    }

    async function exportAsDicomSeg() {
        const seg = (exportOptions && exportOptions.seg) || {};
        if (!seg.available) return;

        if (!(await showConfirmationModal(
                "Als DICOM-SEG speichern?",
                `Die Segmentierung wird als neue Serie "${seg.seg_series_description}" zur `
                + `Originalserie${seg.series_description ? ' "' + seg.series_description + '"' : ''} `
                + `in die dcm4chee-Datenbank geschrieben. Der Zugriff wird protokolliert.`))) {
            return;
        }

        showBlockingLoader("Maske wird für den Export vorbereitet...");
        try {
            const blob = await fetchResultBlob('/export_mask_nifti', 'Die Maske');

            showBlockingLoader("Segmentierung wird in die Datenbank geschrieben...");
            const fd = new FormData();
            fd.append('file', blob, (exportOptions.base_name || 'segmentation') + '_mask.nii.gz');
            fd.append('segmentLabel', seg.segment_label || 'Segmentation');
            fd.append('seriesDescription', seg.seg_series_description || 'Interactive Segmentation (SAMinRAD)');
            fd.append('seriesNumber', String(seg.seg_series_number || 5000));
            fd.append('modelName', seg.model_name || 'SAMinRAD');

            const url = GUI_API_PROXY + '/segmentation_upload/'
                      + encodeURIComponent(seg.study_uid) + '/' + encodeURIComponent(seg.series_uid);
            const started = await guiJson(await fetch(url, {
                method: 'POST', body: fd, credentials: 'same-origin'
            }));
            if (!started.job_id) {
                throw new Error(started.error || 'Die API hat den Auftrag nicht angenommen.');
            }

            showBlockingLoader("SEG-Objekt wird erstellt und gespeichert...");
            const message = await waitForApiJob(started.job_id);

            setExportStatus(message, 'success');
            updateStatusMessage("Segmentierung als DICOM-SEG in der Datenbank gespeichert.");
        } catch (err) {
            setExportStatus('Speichern als DICOM-SEG fehlgeschlagen: ' + err.message, 'error');
        } finally {
            hideBlockingLoader();
        }
    }

    // Event-Handler für die neuen Buttons
    if (btnDownloadResults) {
        btnDownloadResults.addEventListener('click', openExportModal);
    }

    document.querySelectorAll('.export-option').forEach((btn) => {
        btn.addEventListener('click', function() {
            if (btn.disabled) return;
            switch (btn.dataset.export) {
                case 'download':
                    // Der einzige Weg, der ohne das GUI auskommt.
                    window.location.href = '/download_latest_segmentation';
                    updateStatusMessage("Download der Segmentierungsergebnisse gestartet.");
                    closeExportModal();
                    break;
                case 'irods':
                    exportToIrods();
                    break;
                case 'seg':
                    exportAsDicomSeg();
                    break;
            }
        });
    });

    (function initExportModal() {
        const overlay = document.getElementById('export-modal-overlay');
        const closeBtn = document.getElementById('export-modal-close');
        if (closeBtn) closeBtn.addEventListener('click', closeExportModal);
        if (overlay) overlay.addEventListener('click', (e) => {
            if (e.target === overlay) closeExportModal();
        });
    })();
    
    // Funktion zum Neuladen der Anwendung mit denselben Daten
    async function reloadAppWithSameData() {
        console.log("Lade Anwendung mit denselben Daten neu...");
        showLoading(true);
        
        try {
            // Rufe die aktuelle Konfiguration vom Server ab
            const response = await fetch('/get_current_config');
            const data = await response.json();
            
            if (data.success) {
                console.log("Aktuelle Konfiguration erhalten:", data);
                
                // Setze den Workflow-Status zurück
                currentWorkflowStage = 'wl_select';
                
                // Entferne die 'app-finished' Klasse vom Body
                document.body.classList.remove('app-finished');
                
                // Setze die Annotationen zurück
                annotations = [];
                allAnnotations = {};
                
                // Initialisiere die Anwendung neu mit den aktuellen Daten
                if (data.file_type) fileType = data.file_type;
                if (data.slice_counts) sliceCounts = JSON.parse(data.slice_counts);
                if (data.image_filenames) dicomFilenames = JSON.parse(data.image_filenames);
                if (data.base_filename) baseVolumeFilename = data.base_filename;
                
                // Aktualisiere die UI
                updateUIForStage();
                
                // Lade das erste Bild
                loadImage(0, 'axial');
                
                updateStatusMessage("Fensterung kann fortgesetzt werden.");
            } else {
                throw new Error(data.error || "Fehler beim Abrufen der Konfiguration");
            }
        } catch (error) {
            console.error("Fehler beim Neuladen der Anwendung:", error);
            updateStatusMessage("Fehler beim Neuladen: " + error.message);
        } finally {
            showLoading(false);
        }
    }

   if (btnRetrySegmentation) {
    btnRetrySegmentation.addEventListener('click', async function(event) {
        // 1. Add the 'event' parameter to the function signature
        event.preventDefault(); 
        console.log("Fensterung wiederholen Button geklickt. Setze Session zurück.");

        // 2. Get the target URL BEFORE the 'await' call
        const targetUrl = event.currentTarget.dataset.url;

        showBlockingLoader("Neue Segmentierungs-Session wird vorbereitet...");
            resetComparisonMetrics();
        // 3. Reset the server's session state. This is the crucial step.
        await resetSegmentationSession();

        // 4. Navigate to the stored URL after the reset is complete.
        // A full page navigation is cleaner than manually resetting the UI.
        if (targetUrl) {
            window.location.href = targetUrl;
        } else {
            console.error("Keine data-url am Retry-Button gefunden!");
            hideBlockingLoader();
        }
    });
}
    
    if (btnNewSegmentationLink) {
        btnNewSegmentationLink.addEventListener('click', function(event) {
            let targetUrl = event.currentTarget.dataset.url;
            // Reverse-Proxy-Praefix der InterACT-GUI uebernehmen: data-url (z.B. "/") wird vom
            // Proxy NICHT umgeschrieben (nur href/src/action + Routennamen), daher hier manuell.
            const proxyMatch = window.location.pathname.match(/^\/proxy\/colleague(?=\/|$)/);
            const prefix = proxyMatch ? proxyMatch[0] : '';
            if (targetUrl) {
                if (prefix && targetUrl.charAt(0) === '/' && targetUrl.indexOf(prefix) !== 0) {
                    targetUrl = prefix + targetUrl;
                }
                window.location.href = targetUrl;
            } else {
                console.error("Keine data-url am 'Neue Segmentierung'-Button gefunden!");
                window.location.href = prefix + '/';
            }
        });
    }


    // Event-Listener für "Zurück zur Fensterung" Buttons
    document.getElementById('btn-back-to-wl-from-select').addEventListener('click', function() {
        // Zeige das Upload-Fenster wieder an
        const uploadContainer = document.querySelector('.upload-container');
        if (uploadContainer) {
            uploadContainer.classList.remove('hidden');
        }
    });

    // Neue Variablen für die Datenquellenauswahl
    const dataSelectionContainer = document.getElementById('data-selection-container');
    const mainContainer = document.querySelector('.main-container');
    const btnUseTestData = document.getElementById('btn-use-test-data');
    const btnUseUpload = document.getElementById('btn-use-upload');
    const testDataSelection = document.getElementById('test-data-selection');
    const testDataList = document.getElementById('test-data-list');
    const uploadContainer = document.querySelector('.upload-container');

    // Verstecke Upload-Container initial
    if (uploadContainer) {
        uploadContainer.classList.add('hidden');
    }

    // Event-Handler für die Datenquellen-Buttons
    if (btnUseTestData) {
        btnUseTestData.addEventListener('click', async function() {
            try {
                const response = await fetch('/list_test_data');
                const data = await response.json();
                
                if (data.success && data.files) {
                    testDataList.innerHTML = '';
                    data.files.forEach(file => {
                        const item = document.createElement('div');
                        item.className = 'test-data-item';
                        item.textContent = file;
                        item.addEventListener('click', () => loadTestData(file));
                        testDataList.appendChild(item);
                    });
                    testDataSelection.classList.remove('hidden');
                    if (uploadContainer) uploadContainer.classList.add('hidden');
                }
            } catch (error) {
                console.error('Fehler beim Laden der Testdaten:', error);
            }
        });
    }

    if (btnUseUpload) {
        btnUseUpload.addEventListener('click', function() {
            testDataSelection.classList.add('hidden');
            if (uploadContainer) {
                uploadContainer.classList.remove('hidden');
            }
        });
    }

    async function loadTestData(filename) {
        try {
            const response = await fetch(`/load_test_data/${encodeURIComponent(filename)}`);
            const data = await response.json();
            
            if (data.success) {
                // Verstecke Auswahlbereich und zeige Hauptanwendung
                dataSelectionContainer.classList.add('hidden');
                mainContainer.classList.remove('hidden');
                
                // Aktualisiere die Anwendung mit den geladenen Daten
                initialImageFiles = data.image_filenames;
                initialFileType = data.file_type;
                initialSliceCounts = data.slice_counts;
                initialBaseFilename = data.base_filename;
                
                // Starte die Anwendung neu
                initializeApp();
            }
        } catch (error) {
            console.error('Fehler beim Laden der Testdaten:', error);
        }
    }

    // Modifiziere die Upload-Erfolg-Behandlung
    function handleUploadSuccess() {
        // Verstecke Auswahlbereich und zeige Hauptanwendung
        dataSelectionContainer.classList.add('hidden');
        mainContainer.classList.remove('hidden');
        
        // Lade die Seite neu, um die hochgeladenen Daten zu laden
        window.location.reload();
    }

    // === Few-Shot Maskenliste dynamisch füllen ===
    function updateFewshotMaskList() {
        const maskListDiv = document.getElementById('fewshot-mask-list');
        if (!maskListDiv) return;
        
        fetch('/list_fewshot_masks')
            .then(response => response.json())
            .then(data => {
                maskListDiv.innerHTML = '';
                if (data.masks && data.masks.length > 0) {
                    // Gruppiere Masken nach Typ
                    const samMasks = data.masks.filter(m => m.type === 'sam');
                    const fewshotMasks = data.masks.filter(m => m.type === 'few_shot');
                    
                    // Zeige SAM-Masken an
                    if (samMasks.length > 0) {
                        const samHeader = document.createElement('div');
                        samHeader.innerHTML = '<strong>SAM-Masken (Support):</strong>';
                        samHeader.style.marginTop = '10px';
                        samHeader.style.marginBottom = '5px';
                        samHeader.style.color = 'var(--text)';
                        maskListDiv.appendChild(samHeader);

                        if (samMasks.length >= 2) {
                            const toggleBtn = document.createElement('button');
                            toggleBtn.type = 'button';
                            toggleBtn.className = 'action-button';
                            toggleBtn.style.cssText = 'margin: 0 0 8px 0; padding: 3px 10px; font-size: 0.85em; background-color: #546E7A; color: #ffffff;';
                            const updateLabel = () => {
                                const boxes = maskListDiv.querySelectorAll('.fewshot-mask-checkbox');
                                const allChecked = boxes.length > 0 && Array.from(boxes).every(cb => cb.checked);
                                toggleBtn.textContent = allChecked ? 'Auswahl leeren' : 'Alle auswählen';
                            };
                            toggleBtn.addEventListener('click', () => {
                                const boxes = maskListDiv.querySelectorAll('.fewshot-mask-checkbox');
                                const allChecked = boxes.length > 0 && Array.from(boxes).every(cb => cb.checked);
                                boxes.forEach(cb => { cb.checked = !allChecked; });
                                updateLabel();
                                updateFewshotButtonState();
                            });
                            maskListDiv.appendChild(toggleBtn);
                            maskListDiv._samToggleUpdateLabel = updateLabel;
                        }

                        samMasks.forEach(mask => {
                            const checkbox = document.createElement('input');
                            checkbox.type = 'checkbox';
                            checkbox.value = mask.filename;
                            checkbox.className = 'fewshot-mask-checkbox';
                            checkbox.id = 'fewshot_' + mask.filename;
                            checkbox.onchange = () => {
                                updateFewshotButtonState();
                                if (typeof maskListDiv._samToggleUpdateLabel === 'function') {
                                    maskListDiv._samToggleUpdateLabel();
                                }
                            };
                            
                            const label = document.createElement('label');
                            label.htmlFor = checkbox.id;
                            label.innerText = mask.display_name;
                            label.style.marginLeft = '5px';
                            label.style.color = 'var(--text-dim)';
                            
                            const container = document.createElement('div');
                            container.style.marginBottom = '3px';
                            container.appendChild(checkbox);
                            container.appendChild(label);
                            maskListDiv.appendChild(container);
                        });
                    }
                    
                    // Statistik anzeigen
                    const statsDiv = document.createElement('div');
                    statsDiv.style.marginTop = '10px';
                    statsDiv.style.fontSize = '0.8em';
                    statsDiv.style.color = 'var(--text-muted)';
                    statsDiv.innerHTML = `Gesamt: ${data.total_count} Masken (${data.sam_count} SAM, ${data.fewshot_count} Few-Shot)`;
                    maskListDiv.appendChild(statsDiv);
                    
                } else {
                    maskListDiv.innerHTML = '<em>Keine segmentierten Masken vorhanden. Segmentieren Sie zuerst einige Slices mit SAM.</em>';
                }
                updateFewshotButtonState();
            })
            .catch(error => {
                console.error('Fehler beim Laden der Few-Shot-Masken:', error);
                maskListDiv.innerHTML = '<em>Fehler beim Laden der Masken.</em>';
                updateFewshotButtonState();
            });
    }
    
    function updateFewshotButtonState() {
        // Referenzen zu den KORREKTEN Buttons
        const btnInterpolation = document.getElementById('start-fewshot-interpolation-btn');
        const btnMemory = document.getElementById('start-memory-btn');
        const btnS1SFQ = document.getElementById('start-s1sfq-btn');

        if (!btnInterpolation || !btnMemory || !btnS1SFQ) {
            console.warn("Few-Shot-Buttons nicht im DOM gefunden.");
            return;
        }

        const checkedMasks = document.querySelectorAll('.fewshot-mask-checkbox:checked');
        const numChecked = checkedMasks.length;

        // Logik für den Interpolations-Button: mind. 1 Maske
        btnInterpolation.disabled = numChecked === 0;

        // Logik für den Memory-Button: mind. 1 Maske
        btnMemory.disabled = numChecked === 0;

        // Logik für den S1SFQ-Button: genau 1 Maske
        btnS1SFQ.disabled = numChecked !== 1;

        // Optional: Text-Update für Klarheit
        if (numChecked > 0) {
            btnInterpolation.textContent = `Interpolation (${numChecked})`;
            btnMemory.textContent = `Memory (${numChecked})`;
            btnS1SFQ.textContent = `S1SFQ (${numChecked})`;
        } else {
            btnInterpolation.textContent = 'Interpolation';
            btnMemory.textContent = 'Memory';
            btnS1SFQ.textContent = 'S1SFQ';
        }
    }
    
    // === Logik für Few-Shot-Segmentierung ===
    const btnStartFewshot = document.getElementById('start-fewshot-btn');
    if (btnStartFewshot) {
        btnStartFewshot.addEventListener('click', async () => {
            const checkedMasks = document.querySelectorAll('.fewshot-mask-checkbox:checked');
            if (checkedMasks.length === 0) {
                alert("Bitte wählen Sie mindestens eine segmentierte Maske als Beispiel (Support) aus.");
                return;
            }

            if (await showConfirmationModal("Vorlage anwenden?", `Soll die Vorlage auf ${checkedIndices.length} Slices angewendet werden? Dies überschreibt bestehende Annotationen auf diesen Slices.`))
            {
                return;
            }

            const supportMaskFiles = Array.from(checkedMasks).map(cb => cb.value);
            lastSupportMaskFiles = supportMaskFiles.slice();
            const axisForFewshot = sam2FixatedAxis || currentAxis;

            // NEU: Unbestimmten Ladebalken anzeigen
            showProgress("Few-Shot-Segmentierung läuft... Dieser Vorgang kann einige Zeit dauern.", true);

            try {
                const response = await fetch('/few_shot_segment', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(addSliceRangeToPayload({
                        support_masks: supportMaskFiles,
                        axis: axisForFewshot,
                        ww: fixedWW,
                        wl: fixedWL
                    }))
                });
                const result = await response.json();

                if (!response.ok || result.error) {
                    throw new Error(result.error || `Serverfehler ${response.status}`);
                }

                if(statusMessage) statusMessage.textContent = result.message;
                
                // UI nach Erfolg aktualisieren
                await updateFewshotMaskList();
                updateProgressDisplay();
                await loadImage(currentIndex, currentAxis);
                
                if (await showConfirmationModal("Interpolation abgeschlossen", result.message + "\n\nMöchten Sie jetzt die finale 3D-Maske erstellen?")) {
                    await triggerFinishSequence('sam', axisForFewshot);
                } 

            } catch (error) {
                console.error("Fehler bei der Few-Shot-Segmentierung:", error);
                if(statusMessage) statusMessage.textContent = `Fehler: ${error.message}`;
                alert(`Fehler bei der Few-Shot-Segmentierung: ${error.message}`);
            } finally {
                // NEU: Ladebalken immer ausblenden, egal ob Erfolg oder Fehler
                hideProgress();
            }
        });
    }
    
    // === Aufruf beim Laden der Seite ===
    document.addEventListener('DOMContentLoaded', () => {
        updateFewshotMaskList();
    });
  
    // === Fewshot-Button-Referenzen ===
    const btnStartFewshotInterpolation = document.getElementById('start-fewshot-interpolation-btn');
    const btnStartMemory = document.getElementById('start-memory-btn');
    const btnStartS1SFQ = document.getElementById('start-s1sfq-btn');

    // === Event-Listener für Interpolations-Fewshot ===
    if (btnStartFewshotInterpolation) {
        btnStartFewshotInterpolation.addEventListener('click', async () => {
            const checkedMasks = document.querySelectorAll('.fewshot-mask-checkbox:checked');
            if (checkedMasks.length === 0) {
                alert("Bitte wählen Sie mindestens eine segmentierte Maske als Beispiel (Support) aus.");
                return;
            }
            await showSimpleAlert(
            "Interpolations-Segmentierung wird gestartet",
            "Die Segmentierung wird jetzt durchgeführt und anschließend das 3D-Modell erzeugt.\nDieser Vorgang kann einige Zeit dauern."
            );
            const supportMaskFiles = Array.from(checkedMasks).map(cb => cb.value);
            lastSupportMaskFiles = supportMaskFiles.slice();
            const axisForFewshot = sam2FixatedAxis || currentAxis;
    
            let lowerThreshForFewShot = null;
            let upperThreshForFewShot = null;
    
            // NEW: Fetch saved parameters if refinement is enabled
            if (fewShotThresholdEnabled) {
                showBlockingLoader("Lade gespeicherte Threshold-Parameter...");
                try {
                    const paramsResponse = await fetch('/get_hybrid_params');
                    if (paramsResponse.ok) {
                        const data = await paramsResponse.json();
                        if (data.success) {
                            lowerThreshForFewShot = data.hybrid_params.threshold_lower;
                            upperThreshForFewShot = data.hybrid_params.threshold_upper;
                            console.log(`[HYBRID] Geladene Parameter für Few-Shot: ${lowerThreshForFewShot} bis ${upperThreshForFewShot}`);
                        } else {
                            throw new Error(data.error);
                        }
                    } else {
                        throw new Error(`Serverfehler (${paramsResponse.status})`);
                    }
                } catch (error) {
                    hideBlockingLoader();
                    await showSimpleAlert("Fehler", `Konnte gespeicherte Threshold-Parameter nicht laden. Bitte speichern Sie zuerst welche über die 'Verfeinern'-Funktion.\n\nDetails: ${error.message}`);
                    return;
                }
            }

            showBlockingLoader("Interpolation läuft, bitte warten...", { cancellable: true });
            try {
                const response = await fetch('/few_shot_segment_interpolation', {
                    method: 'POST',
                    signal: currentAbortController ? currentAbortController.signal : undefined,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(addSliceRangeToPayload({
                        support_masks: supportMaskFiles,
                        axis: axisForFewshot,
                        ww: fixedWW,
                        wl: fixedWL,
                        // Send fetched or null parameters
                        apply_threshold_refinement: fewShotThresholdEnabled,
                        threshold_lower: lowerThreshForFewShot,
                        threshold_upper: upperThreshForFewShot
                    }))
                });
                const result = await response.json();
                if (!response.ok || result.error) {
                    throw new Error(result.error || `Serverfehler ${response.status}`);
                }
                hideBlockingLoader();
                if(statusMessage) statusMessage.textContent = result.message;
                await updateFewshotMaskList();
                updateProgressDisplay();
                await loadImage(currentIndex, currentAxis);
                await triggerFinishSequence('sam', axisForFewshot, true);
            } catch (error) {
                if (error && error.name === 'AbortError') {
                    if(statusMessage) statusMessage.textContent = "Abgebrochen.";
                    hideBlockingLoader();
                    updateUIForStage();
                    return;
                }
                console.error("Fehler bei der Few-Shot-Segmentierung:", error);
                if(statusMessage) statusMessage.textContent = `Fehler: ${error.message}`;
                alert(`Fehler bei der Few-Shot-Segmentierung: ${error.message}`);
                hideBlockingLoader();
            } finally {
                hideBlockingLoader();
            }
        });
    }

    // === Event-Listener für S1SFQ-Fewshot ===
    if (btnStartS1SFQ) {
        btnStartS1SFQ.addEventListener('click', async () => {
            const checkedMasks = document.querySelectorAll('.fewshot-mask-checkbox:checked');
            if (checkedMasks.length !== 1) {
                alert("Für die S1SFQ-Methode darf genau EINE Support-Maske ausgewählt werden!");
                return;
            }
            await showSimpleAlert(
            "S1SFQ-Segmentierung wird gestartet",
            "Die Segmentierung wird jetzt durchgeführt und anschließend das 3D-Modell erzeugt.\nDieser Vorgang kann einige Zeit dauern."
            );
    
            const supportMaskFiles = Array.from(checkedMasks).map(cb => cb.value);
            lastSupportMaskFiles = supportMaskFiles.slice();
            const axisForFewshot = sam2FixatedAxis || currentAxis;
    
            let lowerThreshForFewShot = null;
            let upperThreshForFewShot = null;
    
            // NEW: Fetch saved parameters if refinement is enabled
            if (fewShotThresholdEnabled) {
                showBlockingLoader("Lade gespeicherte Threshold-Parameter...");
                try {
                    const paramsResponse = await fetch('/get_hybrid_params');
                    if (paramsResponse.ok) {
                        const data = await paramsResponse.json();
                        if (data.success) {
                            lowerThreshForFewShot = data.hybrid_params.threshold_lower;
                            upperThreshForFewShot = data.hybrid_params.threshold_upper;
                            console.log(`[HYBRID] Geladene Parameter für S1SFQ: ${lowerThreshForFewShot} bis ${upperThreshForFewShot}`);
                        } else {
                            throw new Error(data.error);
                        }
                    } else {
                        throw new Error(`Serverfehler (${paramsResponse.status})`);
                    }
                } catch (error) {
                    hideBlockingLoader();
                    await showSimpleAlert("Fehler", `Konnte gespeicherte Threshold-Parameter nicht laden. Bitte speichern Sie zuerst welche über die 'Verfeinern'-Funktion.\n\nDetails: ${error.message}`);
                    return;
                }
            }
    
            showBlockingLoader("S1SFQ-Segmentierung läuft, bitte warten...", { cancellable: true });
            try {
                const response = await fetch('/s1sfq_segment', {
                    method: 'POST',
                    signal: currentAbortController ? currentAbortController.signal : undefined,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(addSliceRangeToPayload({
                        support_masks: supportMaskFiles,
                        axis: axisForFewshot,
                        ww: fixedWW,
                        wl: fixedWL,
                        // Send fetched or null parameters
                        apply_threshold_refinement: fewShotThresholdEnabled,
                        threshold_lower: lowerThreshForFewShot,
                        threshold_upper: upperThreshForFewShot
                    }))
                });
                const result = await response.json();
                if (!response.ok || result.error) {
                    throw new Error(result.error || `Serverfehler ${response.status}`);
                }
                hideBlockingLoader();
                if(statusMessage) statusMessage.textContent = result.message;
                await updateFewshotMaskList();
                updateProgressDisplay();
                await loadImage(currentIndex, currentAxis);
                await triggerFinishSequence('sam', axisForFewshot, true);
            } catch (error) {
                if (error && error.name === 'AbortError') {
                    if(statusMessage) statusMessage.textContent = "Abgebrochen.";
                    hideBlockingLoader();
                    updateUIForStage();
                    return;
                }
                console.error("Fehler bei der S1SFQ-Segmentierung:", error);
                if(statusMessage) statusMessage.textContent = `Fehler: ${error.message}`;
                alert(`Fehler bei der S1SFQ-Segmentierung: ${error.message}`);
                hideBlockingLoader();
            } finally {
                hideBlockingLoader();
            }
        });
    }

    // === Event-Listener für Memory-Attention-Fewshot ===
    if (btnStartMemory) {
        btnStartMemory.addEventListener('click', async () => {
            const checkedMasks = document.querySelectorAll('.fewshot-mask-checkbox:checked');
            if (checkedMasks.length === 0) {
                alert("Bitte wählen Sie mindestens eine segmentierte Maske als Beispiel (Support) aus.");
                return;
            }
            await showSimpleAlert(
                "Memory-Attention-Segmentierung wird gestartet",
                "Die Segmentierung wird jetzt mit SAM2's Memory-Attention durchgeführt und anschließend das 3D-Modell erzeugt.\nDieser Vorgang kann einige Zeit dauern."
            );
            const supportMaskFiles = Array.from(checkedMasks).map(cb => cb.value);
            lastSupportMaskFiles = supportMaskFiles.slice();
            const axisForFewshot = sam2FixatedAxis || currentAxis;

            let lowerThreshForFewShot = null;
            let upperThreshForFewShot = null;

            // Fetch saved parameters if refinement is enabled
            if (fewShotThresholdEnabled) {
                showBlockingLoader("Lade gespeicherte Threshold-Parameter...");
                try {
                    const paramsResponse = await fetch('/get_hybrid_params');
                    if (paramsResponse.ok) {
                        const data = await paramsResponse.json();
                        if (data.success) {
                            lowerThreshForFewShot = data.hybrid_params.threshold_lower;
                            upperThreshForFewShot = data.hybrid_params.threshold_upper;
                            console.log(`[HYBRID] Geladene Parameter für Memory: ${lowerThreshForFewShot} bis ${upperThreshForFewShot}`);
                        } else {
                            throw new Error(data.error);
                        }
                    } else {
                        throw new Error(`Serverfehler (${paramsResponse.status})`);
                    }
                } catch (error) {
                    hideBlockingLoader();
                    await showSimpleAlert("Fehler", `Konnte gespeicherte Threshold-Parameter nicht laden. Bitte speichern Sie zuerst welche über die 'Verfeinern'-Funktion.\n\nDetails: ${error.message}`);
                    return;
                }
            }

            showBlockingLoader("Memory-Attention-Segmentierung läuft, bitte warten...", { cancellable: true });
            try {
                const response = await fetch('/few_shot_segment_memory', {
                    method: 'POST',
                    signal: currentAbortController ? currentAbortController.signal : undefined,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(addSliceRangeToPayload({
                        support_masks: supportMaskFiles,
                        axis: axisForFewshot,
                        ww: fixedWW,
                        wl: fixedWL,
                        apply_threshold_refinement: fewShotThresholdEnabled,
                        threshold_lower: lowerThreshForFewShot,
                        threshold_upper: upperThreshForFewShot,
                        confidence_threshold: 0.0,
                        reinject_high_confidence: false,
                        min_confidence_for_reinject: 0.75
                    }))
                });
                const result = await response.json();
                if (!response.ok || result.error) {
                    throw new Error(result.error || `Serverfehler ${response.status}`);
                }
                hideBlockingLoader();
                if(statusMessage) statusMessage.textContent = result.message;
                await updateFewshotMaskList();
                updateProgressDisplay();
                await loadImage(currentIndex, currentAxis);
                await triggerFinishSequence('sam', axisForFewshot, true);
            } catch (error) {
                if (error && error.name === 'AbortError') {
                    if(statusMessage) statusMessage.textContent = "Abgebrochen.";
                    hideBlockingLoader();
                    updateUIForStage();
                    return;
                }
                console.error("Fehler bei der Memory-Attention-Segmentierung:", error);
                if(statusMessage) statusMessage.textContent = `Fehler: ${error.message}`;
                alert(`Fehler bei der Memory-Attention-Segmentierung: ${error.message}`);
                hideBlockingLoader();
            } finally {
                hideBlockingLoader();
            }
        });
    }


    // === Verbesserte Logik für klickbare Info-Tooltips ===
    function initializeInfoIcons() {
        console.log("Initialisiere Info-Icons...");
        const infoIcons = document.querySelectorAll('.info-icon');
        console.log(`${infoIcons.length} Info-Icons gefunden`);
        
        infoIcons.forEach((icon, index) => {
            console.log(`Info-Icon ${index+1}:`, icon);
            
            // Entferne alte Event-Listener (falls vorhanden)
            const newIcon = icon.cloneNode(true);
            icon.parentNode.replaceChild(newIcon, icon);
            
            // Füge neuen Event-Listener hinzu
            newIcon.addEventListener('click', function(event) {
                console.log(`Info-Icon ${index+1} wurde geklickt!`);
                event.preventDefault();
                event.stopPropagation();
                
                // Debug-Informationen
                console.log("Geklicktes Icon Attribute:", {
                    "data-tooltip-id": newIcon.dataset.tooltipId,
                    "data-modal-title": newIcon.dataset.modalTitle,
                    "data-modal-content": newIcon.dataset.modalContent?.substring(0, 30) + "..."
                });
                
                // Prüfe, ob ein Tooltip oder ein Modal verwendet werden soll
                if (newIcon.dataset.tooltipId) {
                    console.log("Tooltip-Modus aktiviert für ID:", newIcon.dataset.tooltipId);
                    // Tooltip-Logik
                    const tooltipId = newIcon.dataset.tooltipId;
                    const tooltip = document.getElementById(tooltipId);
                    if (!tooltip) {
                        console.error("Tooltip-Element nicht gefunden:", tooltipId);
                        return;
                    }
                    
                    // Alle anderen offenen Tooltips schließen
                    document.querySelectorAll('.custom-tooltip-popup.visible').forEach(openTooltip => {
                        if (openTooltip.id !== tooltipId) {
                            openTooltip.classList.remove('visible');
                        }
                    });
                    
                    // Aktuellen Tooltip anzeigen/verstecken
                    tooltip.classList.toggle('visible');
                    console.log("Tooltip Sichtbarkeit umgeschaltet:", tooltip.classList.contains('visible'));
                    
                    // Positioniere den Tooltip neben dem Icon
                    if (tooltip.classList.contains('visible')) {
                        const iconRect = newIcon.getBoundingClientRect();
                        tooltip.style.left = `${iconRect.right + 5}px`;
                        tooltip.style.top = `${iconRect.top}px`;
                        console.log("Tooltip positioniert bei:", {left: tooltip.style.left, top: tooltip.style.top});
                    }
                } 
                // Wenn modalTitle und modalContent vorhanden sind, zeige ein Modal
                else if (newIcon.dataset.modalTitle && newIcon.dataset.modalContent) {
                    console.log("Modal-Modus aktiviert mit Titel:", newIcon.dataset.modalTitle);
                    showInfoModal(newIcon.dataset.modalTitle, newIcon.dataset.modalContent);
                } else {
                    console.error("Weder tooltip-id noch modal-title/content gefunden!");
                }
            });
        });
    }
    
    // Rufe die Initialisierungsfunktion auf
    initializeInfoIcons();
    
    // === Logik für den Upload von Support-Masken ===
const btnUploadMask = document.getElementById('btn-upload-mask');
const maskUploadInput = document.getElementById('mask-upload-input');
const maskSliceIndexInput = document.getElementById('mask-slice-index-input');

if (btnUploadMask && maskUploadInput && maskSliceIndexInput) {
    btnUploadMask.addEventListener('click', async () => {
        const file = maskUploadInput.files[0];
        const sliceIndexUser = parseInt(maskSliceIndexInput.value, 10); // 1-basiert vom User

        if (!file) {
            alert("Bitte wählen Sie eine Masken-Datei (PNG) aus.");
            return;
        }
        if (isNaN(sliceIndexUser) || sliceIndexUser < 1) {
            alert("Bitte geben Sie eine gültige Slice-Nummer an (>= 1).");
            return;
        }

        const sliceIndex = sliceIndexUser - 1; // Konvertiere zu 0-basiertem Index für Backend
        const axisForUpload = sam2FixatedAxis || currentAxis;

        const formData = new FormData();
        formData.append('mask_file', file);
        formData.append('slice_index', sliceIndex);
        formData.append('axis', axisForUpload);

        showBlockingLoader("Maske wird hochgeladen und verarbeitet...");

        try {
            const response = await fetch('/upload_support_mask', {
                method: 'POST',
                body: formData
            });

            const result = await response.json();

            if (!response.ok || result.error) {
                throw new Error(result.error || `Serverfehler ${response.status}`);
            }

            if (statusMessage) statusMessage.textContent = result.message;

            // Aktualisiere die Liste der verfügbaren Masken
            updateFewshotMaskList();

        } catch (error) {
            console.error("Fehler beim Hochladen der Maske:", error);
            if (statusMessage) statusMessage.textContent = `Fehler: ${error.message}`;
            alert(`Fehler beim Hochladen der Maske: ${error.message}`);
        } finally {
            hideBlockingLoader();
            maskUploadInput.value = ''; // Input zurücksetzen
        }
    });
}

    // Definiere die showInfoModal-Funktion innerhalb des DOMContentLoaded-Events
    window.showInfoModal = function(title, content) {
        console.log("showInfoModal aufgerufen mit:", {title, contentLength: content?.length});
        
        // Entferne vorhandene Modals
        const existingModal = document.getElementById('info-modal');
        if (existingModal) {
            console.log("Vorhandenes Modal entfernt");
            existingModal.remove();
        }
        
        // Erstelle das Modal-Element
        const modal = document.createElement('div');
        modal.id = 'info-modal';
        modal.className = 'modal-overlay';
        modal.innerHTML = `
            <div class="modal-content">
                <div class="modal-header">
                    <h3>${title}</h3>
                    <button class="close-modal">&times;</button>
                </div>
                <div class="modal-body">
                    ${content}
                </div>
            </div>
        `;
        
        // Füge das Modal zum Body hinzu
        document.body.appendChild(modal);
        console.log("Neues Modal zum Body hinzugefügt");
        
        // Event-Listener für das Schließen des Modals
        modal.querySelector('.close-modal').addEventListener('click', () => {
            console.log("Modal durch Close-Button geschlossen");
            modal.remove();
        });
        
        // Schließen bei Klick außerhalb des Modal-Inhalts
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                console.log("Modal durch Klick außerhalb geschlossen");
                modal.remove();
            }
        });
        
        // Schließen mit Escape-Taste
        document.addEventListener('keydown', function escapeHandler(e) {
            if (e.key === 'Escape') {
                console.log("Modal durch Escape-Taste geschlossen");
                modal.remove();
                document.removeEventListener('keydown', escapeHandler);
            }
        });
    };

    if (jsonUploadInput) {
    jsonUploadInput.addEventListener('change', async (event) => {
        const file = event.target.files[0];
        // Input zurücksetzen, damit dieselbe Datei erneut ausgewählt werden kann
        event.target.value = null;
        if (!file) return;

        const reader = new FileReader();
        reader.onerror = () => {
            alert('Fehler beim Lesen der Datei.');
            console.error("File Reader Error:", reader.error);
        };
        reader.onload = async (e) => {
            let settings;
            try {
                settings = JSON.parse(e.target.result);
            } catch (error) {
                alert('Fehler: Die ausgewählte Datei ist keine gültige JSON-Datei.');
                console.error("JSON Parse Error:", error);
                return;
            }
            const options = await showJsonImportModal(settings, file.name);
            if (!options) return; // Abbrechen gedrückt
            await applySettingsFromJson(settings, options);
        };
        reader.readAsText(file);
    });
}

async function handleGroundTruthUpload(formData) {
    if (!niivueViewer || niivueViewer.volumes.length === 0) {
        alert("Bitte erstellen Sie zuerst ein 3D-Modell, bevor Sie eine Ground Truth hochladen.");
        return;
    }

    // Modus abfragen (Schnell vs. Genau) und mitschicken
    const mode = await askComparisonMode();
    if (!mode) return;  // Abgebrochen
    formData.append('precision', mode);

    showBlockingLoader(mode === 'fast' ? "Schneller Vergleich läuft…" : "Genauer Vergleich läuft…");

    // Extrahiere den Dateinamen des aktuell angezeigten Modells
    const generatedMaskUrl = niivueViewer.volumes[0].url;
    const generatedMaskFilename = generatedMaskUrl.substring(generatedMaskUrl.lastIndexOf('/') + 1);
    formData.append('generated_mask_filename', generatedMaskFilename);

    try {
        const response = await fetch('/compare_ground_truth', {
            method: 'POST',
            body: formData,
        });

        const result = await response.json();
        if (!response.ok || result.error) {
            throw new Error(result.error || `Serverfehler ${response.status}`);
        }

        // Metriken anzeigen
        displayComparisonMetrics({ 
            dice: result.dice_score, 
            assd: result.assd_mm, 
            volume_diff: result.volume_diff_cm3,
            sensitivity_percent: result.sensitivity_percent,
            specificity_percent: result.specificity_percent,
            precision_percent: result.precision_percent,    
        });

        // Kombinierte Vergleichs-Ansicht laden (grün=Segmentierung, rot=GT, gelb=Überlappung)
        await loadGroundTruthOverlay(result.comparison_overlay_url);

        // Rückmeldung zur STL-Ausrichtung anzeigen (ob die Umwandlung passt)
        if (result.alignment_note) {
            const q = result.alignment_quality;
            const title = (q === 'unsicher') ? '⚠️ STL-Ausrichtung prüfen'
                        : (q === 'maessig') ? 'STL-Ausrichtung – bitte prüfen'
                        : 'STL-Ausrichtung';
            await showSimpleAlert(title, result.alignment_note);
        }

    } catch (error) {
        console.error("Fehler beim Vergleich:", error);
        alert(`Fehler beim Vergleich: ${error.message}`);
    } finally {
        hideBlockingLoader();
        // Setze die Input-Felder und die Anzeige zurück
        if (gtFileInput) gtFileInput.value = null;
        if (gtFileNameDisplay) gtFileNameDisplay.style.display = 'none';
    }
}

// Event-Listener für den Einzeldatei-Upload
if (gtFileInput) {
    gtFileInput.addEventListener('change', (event) => {
        const file = event.target.files[0];
        if (!file) return;

        // Anzeige aktualisieren
        if (gtFileNameDisplay) {
            gtFileNameDisplay.textContent = `Ausgewählte Datei: ${file.name}`;
            gtFileNameDisplay.style.display = 'block';
        }

        const formData = new FormData();
        formData.append('ground_truth_file', file);
        handleGroundTruthUpload(formData);
    });
}

// (DICOM-Ordner-Upload für Ground-Truth entfernt – es werden nur Einzeldateien verglichen.)

const btnBackToAnnotation = document.getElementById('btn-back-to-annotation');
if (btnBackToAnnotation) {
    btnBackToAnnotation.addEventListener('click', async () => {
        if (currentWorkflowStage !== 'finished' || !segmentationMethod) return;

        if (!(await showConfirmationModal("Annotation wiederholen?", "Dadurch werden das erstellte 3D-Modell und alle bisherigen Ergebnisse dieser Sitzung gelöscht. Möchten Sie fortfahren?"))) {
            return; // Benutzer hat abgebrochen
        }
        resetComparisonMetrics();
        await resetSegmentationSession();

        annotations = [];
        allAnnotations = {};
        lastSupportMaskFiles = null;
        savedIndicesPerAxis = {};
        savedMethodPerSlice = {};
        currentSegmentationUrl = null;
        isShowingSegmentation = false;
        
        // UI-Elemente zurücksetzen, die vom alten Zustand abhängen
        if (btnUndo) btnUndo.disabled = true;
        if (btnViewOriginal) btnViewOriginal.classList.add('active');
        if (btnViewSegmentation) {
            btnViewSegmentation.classList.remove('active');
            btnViewSegmentation.disabled = true;
        }

        // Setze den Workflow-Status auf die vorherige Annotationsstufe
        if (segmentationMethod === 'sam') {
            currentWorkflowStage = 'annotate_sam';
        } else if (segmentationMethod === 'threshold') {
            currentWorkflowStage = 'annotate_thresh';
        } else {
            currentWorkflowStage = 'method_select'; // Fallback
        }

        // Setze den 3D-Viewer zurück
        if (niivueViewer) {
            try {
                niivueViewer.detach();
            } catch (e) {
                console.warn("Fehler beim detach des Niivue Viewers:", e);
            }
            niivueViewer = null;
        }

        // KORREKTUR: Setze den Index auf einen gültigen Wert zurück,
        // bevor das Bild neu geladen wird, um eine Endlosschleife zu vermeiden.
        const axisToReturnTo = sam2FixatedAxis || currentAxis;
        const totalSlices = isVolumetric() ? (sliceCounts[axisToReturnTo] || 0) : dicomFilenames.length;
        if (currentIndex >= totalSlices && totalSlices > 0) {
            currentIndex = totalSlices - 1; // Setze auf den letzten gültigen Slice
        } else if (totalSlices === 0) {
            currentIndex = 0; // Fallback
        }

        // Lade das Bild neu. Die Funktion kümmert sich um die UI-Updates.
        loadImage(currentIndex, currentAxis);

        if (statusMessage) statusMessage.textContent = "Zurück in der Annotations-Stufe.";
    });
}
if (finalSliceSlider) {
        finalSliceSlider.addEventListener('input', (event) => {
            const newIndex = parseInt(event.target.value, 10);
            if (!isNaN(newIndex)) {
                loadFinalOverlayImage(newIndex);
                updateFinalSliceSliderLabel(newIndex);
            }
        });
    }

function updateVoxelPreviewSize() {
    const isSamActive = currentWorkflowStage === 'annotate_sam';
    const checkbox = isSamActive ? cbShowVoxelSizeSam : cbShowVoxelSizeThresh;
    const input = isSamActive ? inputMinOutlierSizeSam : inputMinOutlierSizeThresh;

    if (!checkbox || !input) {
        voxelSizePreview.show = false;
        return;
    }

    voxelSizePreview.show = checkbox.checked;
    if (voxelSizePreview.show) {
        const voxels = parseFloat(input.value) || 0;
        // Math.cbrt() ist die JavaScript-Funktion für die dritte Wurzel
        voxelSizePreview.size = voxels > 0 ? Math.cbrt(voxels) : 0;
    } else {
        voxelSizePreview.size = 0;
    }

    // Canvas neu zeichnen, um die Änderung (Quadrat anzeigen/verstecken/anpassen) darzustellen
    drawImageAndAnnotations();
}

function resetComparisonMetrics() {
        // GT-Vergleich beendet -> Legende zurücksetzen
        window.gtComparisonActive = false;
        updateGtLegendVisibility();

        if (!metricsDisplayDiv) return;

        // Verstecke den gesamten Container
        metricsDisplayDiv.classList.add('hidden');
        
        // Setze alle Text-Anzeigen zurück
        if (diceScoreDisplay) diceScoreDisplay.textContent = '-';
        const assdDisplay = document.getElementById('assd-display');
        if (assdDisplay) assdDisplay.textContent = '-';
        const volumeDiffDisplay = document.getElementById('volume-diff-display');
        if (volumeDiffDisplay) volumeDiffDisplay.textContent = '-';
        const sensitivityDisplay = document.getElementById('sensitivity-display');
        if (sensitivityDisplay) sensitivityDisplay.textContent = '-';
        const specificityDisplay = document.getElementById('specificity-display');
        if (specificityDisplay) specificityDisplay.textContent = '-';
        const precisionDisplay = document.getElementById('precision-display');
        if (precisionDisplay) precisionDisplay.textContent = '-';
        
        // Setze den Dateinamen-Display zurück
        if (gtFileNameDisplay) {
            gtFileNameDisplay.style.display = 'none';
            gtFileNameDisplay.textContent = '';
        }
        
        console.log("Ground-Truth-Vergleichsmetriken wurden zurückgesetzt.");
    }

// NEU: Event-Listener für die neuen Checkboxen und die bestehenden Eingabefelder
if (cbShowVoxelSizeSam) cbShowVoxelSizeSam.addEventListener('change', updateVoxelPreviewSize);
if (inputMinOutlierSizeSam) inputMinOutlierSizeSam.addEventListener('input', updateVoxelPreviewSize);

if (cbShowVoxelSizeThresh) cbShowVoxelSizeThresh.addEventListener('change', updateVoxelPreviewSize);
if (inputMinOutlierSizeThresh) inputMinOutlierSizeThresh.addEventListener('input', updateVoxelPreviewSize);

// HYBRID-ANSATZ: Event-Listener einrichten
setupHybridEventListeners();

// ==================== SPIELMODUS-FUNKTIONEN ====================

function handleGameModeCompletion(gtComparisonData, weightData) {
    console.log("Spielmodus: Verarbeite GT-Vergleich", gtComparisonData);

    // Zeige GT-Vergleichsmetriken an (mit korrekten Parameternamen)
    displayComparisonMetrics({
        dice: gtComparisonData.dice_score,
        assd: gtComparisonData.assd_mm,
        volume_diff: gtComparisonData.volume_diff_percent,
        sensitivity_percent: gtComparisonData.sensitivity_percent,
        specificity_percent: gtComparisonData.specificity_percent,
        precision_percent: gtComparisonData.precision_percent
    });

    // Speichere GT-Daten für Leaderboard
    window.gameModeGTData = gtComparisonData;
    window.gameModeWeightData = weightData;

    // Lade kombinierte Vergleichs-Ansicht im 3D-Viewer
    if (gtComparisonData.comparison_overlay_url) {
        loadGroundTruthOverlay(gtComparisonData.comparison_overlay_url).catch(err => {
            console.error("Fehler beim Laden der Vergleichs-Ansicht:", err);
        });
    }

    // Zeige Ranglisten-Sektion
    const leaderboardSection = document.getElementById('game-mode-leaderboard-section');
    if (leaderboardSection) {
        leaderboardSection.classList.remove('hidden');
        loadLeaderboard();
    }
}

async function loadLeaderboard() {
    console.log("Lade Rangliste...");
    const tbody = document.getElementById('game-mode-leaderboard-tbody');
    if (!tbody) return;

    try {
        const response = await fetch('/api/leaderboard');
        const result = await response.json();

        if (!response.ok || !result.success) {
            throw new Error(result.error || 'Fehler beim Laden der Rangliste');
        }

        const leaderboard = result.leaderboard || [];
        tbody.innerHTML = '';

        if (leaderboard.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="padding: 20px; text-align: center; color: #6c757d;">Noch keine Einträge. Seien Sie der Erste!</td></tr>';
            return;
        }

        leaderboard.forEach((entry, index) => {
            const row = document.createElement('tr');
            row.style.borderBottom = '1px solid #dee2e6';

            // Rank
            const rankCell = document.createElement('td');
            rankCell.style.padding = '10px';
            rankCell.style.fontWeight = '600';
            rankCell.style.color = index < 3 ? '#667eea' : '#495057';
            rankCell.textContent = index + 1;

            // Name
            const nameCell = document.createElement('td');
            nameCell.style.padding = '10px';
            nameCell.textContent = entry.name || 'Anonym';

            // Dice Score
            const diceCell = document.createElement('td');
            diceCell.style.padding = '10px';
            diceCell.style.textAlign = 'center';
            diceCell.style.fontWeight = '600';
            diceCell.style.color = entry.dice_score >= 0.9 ? '#28a745' : entry.dice_score >= 0.7 ? '#ffc107' : '#dc3545';
            diceCell.textContent = entry.dice_score.toFixed(4);

            // Volume
            const volumeCell = document.createElement('td');
            volumeCell.style.padding = '10px';
            volumeCell.style.textAlign = 'center';
            volumeCell.textContent = entry.volume_cm3 ? entry.volume_cm3.toFixed(2) : '-';

            // Date
            const dateCell = document.createElement('td');
            dateCell.style.padding = '10px';
            dateCell.style.textAlign = 'center';
            dateCell.style.fontSize = '0.85rem';
            dateCell.style.color = '#6c757d';
            dateCell.textContent = entry.timestamp ? entry.timestamp.split(' ')[0] : '-';

            row.appendChild(rankCell);
            row.appendChild(nameCell);
            row.appendChild(diceCell);
            row.appendChild(volumeCell);
            row.appendChild(dateCell);
            tbody.appendChild(row);
        });

        console.log(`Rangliste geladen: ${leaderboard.length} Einträge`);

    } catch (error) {
        console.error('Fehler beim Laden der Rangliste:', error);
        tbody.innerHTML = '<tr><td colspan="5" style="padding: 20px; text-align: center; color: #dc3545;">Fehler beim Laden der Rangliste</td></tr>';
    }
}

async function saveToLeaderboard(playerName, diceScore, volumeCm3, assdMm, sensitivity, precision, specificity) {
    console.log("Speichere in Rangliste:", playerName, diceScore);

    try {
        const response = await fetch('/api/leaderboard', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: playerName,
                dice_score: diceScore,
                volume_cm3: volumeCm3,
                assd_mm: assdMm,
                sensitivity: sensitivity,
                precision: precision,
                specificity: specificity
            })
        });

        const result = await response.json();

        if (!response.ok || !result.success) {
            throw new Error(result.error || 'Fehler beim Speichern');
        }

        console.log("Erfolgreich in Rangliste eingetragen!");
        return result;

    } catch (error) {
        console.error('Fehler beim Speichern in Rangliste:', error);
        throw error;
    }
}

// Event-Listener für Spielmodus-Buttons
const btnSaveToLeaderboard = document.getElementById('btn-save-to-leaderboard');
const gameModePlayerName = document.getElementById('game-mode-player-name');
const gameModeSaveStatus = document.getElementById('game-mode-save-status');

if (btnSaveToLeaderboard) {
    btnSaveToLeaderboard.addEventListener('click', async () => {
        const playerName = gameModePlayerName ? gameModePlayerName.value.trim() : '';

        if (!playerName) {
            if (gameModeSaveStatus) {
                gameModeSaveStatus.textContent = 'Bitte geben Sie einen Namen ein!';
                gameModeSaveStatus.style.color = '#dc3545';
            }
            return;
        }

        if (!window.gameModeGTData) {
            if (gameModeSaveStatus) {
                gameModeSaveStatus.textContent = 'Keine Vergleichsdaten vorhanden!';
                gameModeSaveStatus.style.color = '#dc3545';
            }
            return;
        }

        btnSaveToLeaderboard.disabled = true;
        btnSaveToLeaderboard.textContent = 'Speichere...';

        try {
            await saveToLeaderboard(
                playerName,
                window.gameModeGTData.dice_score,
                window.gameModeWeightData ? window.gameModeWeightData.volume_cm3 : null,
                window.gameModeGTData.assd_mm,
                window.gameModeGTData.sensitivity_percent,
                window.gameModeGTData.precision_percent,
                window.gameModeGTData.specificity_percent
            );

            if (gameModeSaveStatus) {
                gameModeSaveStatus.textContent = '✓ Erfolgreich in Rangliste eingetragen!';
                gameModeSaveStatus.style.color = '#28a745';
            }

            // Aktualisiere Rangliste
            await loadLeaderboard();

            // Deaktiviere Input und Button
            if (gameModePlayerName) gameModePlayerName.disabled = true;
            btnSaveToLeaderboard.textContent = 'Gespeichert';

        } catch (error) {
            if (gameModeSaveStatus) {
                gameModeSaveStatus.textContent = `Fehler: ${error.message}`;
                gameModeSaveStatus.style.color = '#dc3545';
            }
            btnSaveToLeaderboard.disabled = false;
            btnSaveToLeaderboard.textContent = 'In Rangliste eintragen';
        }
    });
}

// ==================== ENDE SPIELMODUS-FUNKTIONEN ====================

}); // Ende DOMContentLoaded