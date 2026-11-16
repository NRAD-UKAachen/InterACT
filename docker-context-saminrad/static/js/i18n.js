/*
 * i18n.js – Manueller Sprachumschalter DE/EN für SAMinRAD.
 *
 * Ansatz (ohne Umbau der bestehenden Templates/script_dicom.js):
 *  - Ein Wörterbuch DICT (Deutsch -> Englisch) für alle statischen Strings.
 *  - RULES: Regex-Regeln für dynamische Strings mit Zahlen/Variablen.
 *  - Ein Text-Node-Walker übersetzt beim Laden das komplette Dokument.
 *  - Ein MutationObserver übersetzt anschließend dynamisch erzeugte Texte
 *    (Status, Modals, Listen) automatisch in die aktive Sprache.
 *  - window.alert wird umschlossen, da Browser-Dialoge keine DOM-Knoten sind.
 *  - Reversibel über eine WeakMap (Original-Deutsch je Textknoten).
 *
 * Startsprache: Englisch. Der Wechsel gilt nur für die Sitzung (nicht gemerkt).
 */
(function () {
    "use strict";

    var currentLang = 'en';                 // Startsprache
    var CANON = new WeakMap();              // Textknoten -> kanonischer deutscher Text (trimmed)
    var LASTW = new WeakMap();              // Textknoten -> zuletzt von uns geschriebener Wert (Feedback-Schutz)
    var TITLE_CANON;                        // Original document.title

    // ---- Wörterbuch Deutsch -> Englisch (statische Strings) -------------------
    var PAIRS = [
        // Kopf / Seitentitel
        ["Interaktive Segmentierung medizinischer Bilddaten", "Interactive Segmentation of Medical Image Data"],
        ["Interaktive Segmentierung medizinischer Bilddaten - Volumetrie", "Interactive Segmentation of Medical Image Data - Volumetry"],
        ["Interaktive Segmentierung - Datenquelle wählen", "Interactive Segmentation - Select Data Source"],

        // Workflow-Stepper
        ["Fensterung", "Windowing"],
        ["Methode", "Method"],
        ["Annotation", "Annotation"],
        ["Ergebnis", "Result"],

        // Kopfzeilen / Überschriften
        ["Ansicht wählen:", "Select view:"],
        ["Stufe 1: Fensterung", "Stage 1: Windowing"],
        ["Segmentierungsmethode wählen", "Select segmentation method"],
        ["Stufe 2a: Annotieren (SAM)", "Stage 2a: Annotate (SAM)"],
        ["Stufe 2b: Thresholding", "Stage 2b: Thresholding"],
        ["3D Masken-Ansicht:", "3D mask view:"],
        ["2D Overlay-Ansicht:", "2D overlay view:"],
        ["Nachbearbeitung (Optional):", "Post-processing (optional):"],
        ["Material-Dichte (Optional):", "Material density (optional):"],
        ["Eigene Support-Maske hochladen:", "Upload your own support mask:"],
        ["Volumetrische Analyse:", "Volumetric analysis:"],
        ["Ergebnis-Aktionen:", "Result actions:"],
        ["Ground-Truth-Vergleich:", "Ground-truth comparison:"],
        ["Vergleichsergebnisse:", "Comparison results:"],
        ["Datenquelle wählen", "Select data source"],

        // Viewer / Ansichts-Toggles
        ["Lädt...", "Loading..."],
        ["Original", "Original"],
        ["Segmentierung", "Segmentation"],
        ["2D Overlay", "2D overlay"],
        ["3D Ansicht wird nach Erstellung geladen...", "3D view will load after creation..."],
        ["GT-Vergleich:", "GT comparison:"],
        ["nur Segmentierung", "segmentation only"],
        ["nur Ground-Truth", "ground truth only"],
        ["Überlappung", "overlap"],
        ["Serie:", "Series:"],
        ["Axial", "Axial"],
        ["Sagittal", "Sagittal"],
        ["Koronal", "Coronal"],

        // Stufe 1: Fensterung
        ["Zurück zum Upload", "Back to upload"],
        ["Einstellungen laden (.json)", "Load settings (.json)"],
        ["Level:", "Level:"],
        ["Width:", "Width:"],
        ["Reset W/L", "Reset W/L"],
        ["Presets:", "Presets:"],
        ["Gehirn", "Brain"],
        ["Stroke", "Stroke"],
        ["Mediastinum", "Mediastinum"],
        ["Abdomen", "Abdomen"],
        ["Lunge", "Lung"],
        ["Knochen", "Bone"],
        ["Weichteil", "Soft tissue"],
        ["Maske", "Mask"],
        ["< Vorheriges", "< Previous"],
        ["Nächstes >", "Next >"],
        ["Weiter >", "Next >"],
        ["Fensterung übernehmen & Methode wählen", "Apply windowing & select method"],

        // Methodenauswahl
        ["Annotieren (SAM)", "Annotate (SAM)"],
        ["Interaktiv mit Punkten/Box – präzise, manuell steuerbar", "Interactive with points/box – precise, manually controllable"],
        ["Thresholding", "Thresholding"],
        ["Automatisch über HU-Schwellenwerte – schnell bei klaren Kontrasten", "Automatic via HU thresholds – fast for clear contrasts"],
        ["Zurück zur Fensterung", "Back to windowing"],

        // Stufe 2a: Annotieren
        ["Fensterung: L=", "Windowing: L="],
        ["Fensterung (nur zur Info): L=", "Windowing (info only): L="],
        ["Methode ändern", "Change method"],
        ["Segmentierungs-Bereich (optional)", "Segmentation range (optional)"],
        ["Von Slice:", "From slice:"],
        ["Bis Slice:", "To slice:"],
        ["Ganze Serie", "Whole series"],
        ["Leer/Reset = gesamte Serie. Slices außerhalb werden auf Null gesetzt.", "Empty/reset = whole series. Slices outside are set to zero."],
        ["Box", "Box"],
        ["Freihand", "Freehand"],
        ["Positiv", "Positive"],
        ["Negativ", "Negative"],
        ["Rückgängig", "Undo"],
        ["Reset Annotationen", "Reset annotations"],
        ["Segmentieren (SAM)", "Segment (SAM)"],
        ["🔬 Mit Thresholding verfeinern", "🔬 Refine with thresholding"],
        ["Vorlagen", "Templates"],
        ["Vorlage speichern", "Save template"],
        ["Vorlage auf Aktuelles", "Template to current"],
        ["Vorlage auf Auswahl...", "Template to selection..."],
        ["Bereich markieren", "Select range"],
        ["Alle auswählen", "Select all"],
        ["Auswahl aufheben", "Deselect all"],
        ["Vorlage auf X Slices anwenden", "Apply template to X slices"],
        ["Restliche überspringen", "Skip remaining"],
        ["Löcher füllen", "Fill holes"],
        ["Ausreißer entfernen", "Remove outliers"],
        ["Vorschau", "Preview"],
        ["Dichte:", "Density:"],
        ["Für Gewichtsberechnung. Beispiele: Wasser = 1.0, Blut = 1.05, Knochen = 1.8", "For weight calculation. Examples: water = 1.0, blood = 1.05, bone = 1.8"],
        ["Verfeinern Sie die SAM-Maske mit pixelgenauer Thresholding-Präzision", "Refine the SAM mask with pixel-precise thresholding"],
        ["Untere Grenze:", "Lower limit:"],
        ["Obere Grenze:", "Upper limit:"],
        ["Threshold-Werte speichern", "Save threshold values"],
        ["Abbrechen", "Cancel"],
        ["Bearbeitung (SAM) abschließen", "Finish editing (SAM)"],
        ["Bearbeitung (Thresh) abschließen", "Finish editing (thresh)"],

        // Few-Shot-Box
        ["Few-Shot-Segmentierung", "Few-shot segmentation"],
        ["Nachdem Sie mit SAM segmentiert haben, können Sie auch die Few-Shot-Mechanik testen.", "After segmenting with SAM, you can also try the few-shot mechanism."],
        ["Wählen Sie Ihre Maske(n) aus, die Sie dafür nutzen wollen:", "Select the mask(s) you want to use for this:"],
        ["🔬 Mit vorher definierten Thresholding-Werten verfeinern", "🔬 Refine with previously defined thresholding values"],
        ["Verwendet die zuletzt in der manuellen SAM-Verfeinerung gesetzten Threshold-Werte.", "Uses the threshold values last set in the manual SAM refinement."],
        ["Few-Shot mit Interpolation", "Few-shot with interpolation"],
        ["Memory", "Memory"],
        ["S1SFQ", "S1SFQ"],
        ["Für Slice:", "For slice:"],
        ["Hochladen", "Upload"],
        ["Die Maske muss eine binäre PNG-Datei sein und exakt die Dimensionen des Zielslices haben.", "The mask must be a binary PNG file and exactly match the dimensions of the target slice."],

        // Stufe 2b: Thresholding
        ["Schwellenwerte (Pixel-/HU-Werte):", "Thresholds (pixel/HU values):"],
        ["Reset Thresholds", "Reset thresholds"],

        // Gewicht / Ergebnis
        ["Volumen:", "Volume:"],
        ["Gewicht:", "Weight:"],
        ["Material-Dichte:", "Material density:"],
        ["Slice auswählen:", "Select slice:"],
        ["Laden Sie Ihre Ground-Truth-Maske (NIfTI, NRRD oder STL) hoch, um sie mit dem erstellten Modell zu vergleichen. STL wird auf das Gitter Ihrer Segmentierung voxelisiert und muss im selben Koordinatensystem wie der Scan liegen.", "Upload your ground-truth mask (NIfTI, NRRD or STL) to compare it with the created model. The STL is voxelized onto the grid of your segmentation and must be in the same coordinate system as the scan."],
        ["Datei auswählen...", "Select file..."],
        ["Dice-Koeffizient:", "Dice coefficient:"],
        ["Durchschn. Oberflächenabstand (ASSD):", "Avg. surface distance (ASSD):"],
        ["Sensitivität (TPR):", "Sensitivity (TPR):"],
        ["Präzision (PPV):", "Precision (PPV):"],
        ["Spezifität (TNR):", "Specificity (TNR):"],
        ["Volumendifferenz:", "Volume difference:"],
        ["Segmentierungsergebnisse herunterladen", "Download segmentation results"],
        ["Zurück", "Back"],
        ["Segmentierung ab Fensterung wiederholen", "Repeat segmentation from windowing"],
        ["Neue Segmentierung starten", "Start new segmentation"],
        ["Initialisiere...", "Initializing..."],

        // Bestätigungs-Modal (Vorgaben)
        ["Bestätigung", "Confirmation"],
        ["Möchten Sie diese Aktion wirklich durchführen?", "Do you really want to perform this action?"],
        ["Bestätigen", "Confirm"],

        // Blocking-Loader
        ["Verarbeite...", "Processing..."],

        // Upload-Seite
        ["Starten Sie, indem Sie Ihre Daten hochladen oder unsere Beispieldaten verwenden.", "Start by uploading your data or using our example data."],
        ["Dateien hierher ziehen", "Drag files here"],
        ["oder wählen Sie eine Quelle aus", "or choose a source"],
        ["Einzelne Datei", "Single file"],
        ["DICOM-Ordner", "DICOM folder"],
        ["Unterstützt:", "Supported:"],
        ["Hochladen & Segmentierung starten", "Upload & start segmentation"],
        ["ODER", "OR"],
        ["Beispieldaten verwenden", "Use example data"],
        ["Ausgewählt:", "Selected:"],
        ["Ausgewählter Ordner:", "Selected folder:"],
        ["(ohne Beschreibung)", "(no description)"],

        // ---- title=-Tooltips -------------------------------------------------
        ["Bearbeitungsfortschritt", "Editing progress"],
        ["Originalaufnahme anzeigen", "Show original image"],
        ["Segmentierung anzeigen", "Show segmentation"],
        ["3D-Ansicht anzeigen", "Show 3D view"],
        ["2D-Overlay anzeigen", "Show 2D overlay"],
        ["DICOM-Serie auswählen", "Select DICOM series"],
        ["Axiale Ansicht", "Axial view"],
        ["Sagittale Ansicht", "Sagittal view"],
        ["Koronale Ansicht", "Coronal view"],
        ["Zurück zum Upload (aktuelle Sitzung wird beendet)", "Back to upload (current session will end)"],
        ["Gespeicherte Segmentierungsparameter aus einer JSON-Datei laden", "Load saved segmentation parameters from a JSON file"],
        ["Level +20", "Level +20"],
        ["Level -20", "Level -20"],
        ["Width +20", "Width +20"],
        ["Width -20", "Width -20"],
        ["W/L auf Standard zurücksetzen", "Reset W/L to default"],
        ["Optimale Ansicht für binäre Masken (Werte 0 und 1)", "Optimal view for binary masks (values 0 and 1)"],
        ["Vorheriges Bild (Pfeil Hoch)", "Previous image (arrow up)"],
        ["Nächstes Bild (Pfeil Runter)", "Next image (arrow down)"],
        ["Zum nächsten Bild (Pfeil Runter)", "To next image (arrow down)"],
        ["Aktuelle Fensterung übernehmen & Methode wählen", "Apply current windowing & select method"],
        ["Interaktive Segmentierung mit Punkten/Box und SAM", "Interactive segmentation with points/box and SAM"],
        ["Segmentierung über Schwellenwerte (Pixel-/HU-Werte etc.)", "Segmentation via thresholds (pixel/HU values etc.)"],
        ["Zurück zur Fensterungseinstellung", "Back to windowing settings"],
        ["Andere Segmentierungsmethode wählen", "Choose a different segmentation method"],
        ["Markiere dein Segment", "Mark your segment"],
        ["Region mit freier Hand zeichnen (Lasso)", "Draw a region freehand (lasso)"],
        ["Soll segmentiert werden", "Should be segmented"],
        ["Soll nicht segmentiert werden", "Should not be segmented"],
        ["Letzte Annotation entfernen (Strg+Z)", "Remove last annotation (Ctrl+Z)"],
        ["Alle Annotationen für dieses Bild zurücksetzen.", "Reset all annotations for this image."],
        ["SAM-Segmentierung für aktuellen Slice durchführen", "Run SAM segmentation for the current slice"],
        ["SAM-Ergebnis mit Thresholding verfeinern", "Refine the SAM result with thresholding"],
        ["Aktuelle Annotationen (Box/Punkte) als Vorlage speichern", "Save current annotations (box/points) as a template"],
        ["Gespeicherte Vorlage auf DIESES aktuelle Bild anwenden (überschreibt aktuelle Zeichnungen!)", "Apply the saved template to THIS current image (overwrites current drawings!)"],
        ["Gespeicherte Vorlage auf ausgewählte Bilder anwenden", "Apply the saved template to selected images"],
        ["Checkboxen im angegebenen Bereich auswählen", "Select checkboxes in the given range"],
        ["Alle übrigen, noch nicht gespeicherten Bilder als 'ohne Segmentierung' markieren und überspringen (für SAM).", "Mark all remaining, not-yet-saved images as 'without segmentation' and skip (for SAM)."],
        ["Füllt Voxel in der Segmentierung, die nur von Positiven umgeben sind.", "Fills voxels in the segmentation that are surrounded only by positives."],
        ["Entfernt Voxel, die als Ausreißer betrachtet werden.", "Removes voxels considered to be outliers."],
        ["Zeigt eine Vorschau der Voxel-Größe als Querschnitt eines Würfels oben rechts im Viewer an.", "Shows a preview of the voxel size as the cross-section of a cube in the top right of the viewer."],
        ["Verfeinerung abbrechen", "Cancel refinement"],
        ["Ganze Serie abschließen (SAM) und 3D-Maske optional erstellen", "Finish the whole series (SAM) and optionally create a 3D mask"],
        ["Nutzt eine bidirektionale Interpolation zwischen den Support-Masken. Gut für gleichmäßige Objekte.", "Uses bidirectional interpolation between the support masks. Good for uniform objects."],
        ["Nutzt SAM2's Memory-Attention für robuste Segmentierung. Besonders gut bei Objekten mit Lücken.", "Uses SAM2's memory attention for robust segmentation. Especially good for objects with gaps."],
        ["Nutzt die S1SFQ-Methode aus dem Paper. Segmentiert von der Mitte nach außen, um die Genauigkeit zu verbessern.", "Uses the S1SFQ method from the paper. Segments from the center outward to improve accuracy."],
        ["Untere Grenze +10", "Lower limit +10"],
        ["Untere Grenze -10", "Lower limit -10"],
        ["Obere Grenze +10", "Upper limit +10"],
        ["Obere Grenze -10", "Upper limit -10"],
        ["Schwellenwerte zurücksetzen", "Reset thresholds"],
        ["Ganze Serie abschließen (Threshold) und 3D-Maske optional erstellen", "Finish the whole series (threshold) and optionally create a 3D mask"],
        ["NIfTI-, NRRD- oder STL-Datei hochladen", "Upload NIfTI, NRRD or STL file"],
        ["Segmentierungsergebnisse als ZIP-Datei herunterladen", "Download segmentation results as a ZIP file"],
        ["Zurück zur Annotations-Stufe, um weitere Änderungen vorzunehmen", "Back to the annotation stage to make further changes"],
        ["Mit denselben Daten eine neue Segmentierung starten", "Start a new segmentation with the same data"],
        ["Komplett neue Segmentierung mit neuen Daten starten", "Start a completely new segmentation with new data"],

        // Platzhalter
        ["z.B. 1.05", "e.g. 1.05"],

        // ---- Info-Modal-Titel ------------------------------------------------
        ["Few-Shot + Thresholding", "Few-shot + thresholding"],
        ["Interpolations-Methode", "Interpolation method"],
        ["Memory-Attention-Methode", "Memory-attention method"],
        ["S1SFQ-Methode", "S1SFQ method"],
        ["Dice-Koeffizient", "Dice coefficient"],
        ["Durchschnittlicher Symmetrischer Oberflächenabstand (ASSD)", "Average Symmetric Surface Distance (ASSD)"],
        ["Sensitivität (True Positive Rate)", "Sensitivity (True Positive Rate)"],
        ["Präzision (Positive Predictive Value)", "Precision (Positive Predictive Value)"],
        ["Spezifität (True Negative Rate)", "Specificity (True Negative Rate)"],
        ["Volumendifferenz", "Volume difference"],

        // ---- Info-Modal-Inhalte (Fragmente) ----------------------------------
        ["Aktivieren Sie diese Option, um die Few-Shot Ergebnisse automatisch mit den zuletzt in der manuellen SAM-Verfeinerung gesetzten Thresholding-Werten zu verfeinern.", "Enable this option to automatically refine the few-shot results with the thresholding values last set in the manual SAM refinement."],
        ["Workflow:", "Workflow:"],
        ["Few-Shot erstellt grobe Masken für alle Slices", "Few-shot creates coarse masks for all slices"],
        ["In jeder Maske wird pixelgenaues Thresholding mit den vordefinierten Werten angewendet", "Pixel-precise thresholding with the predefined values is applied to each mask"],
        ["Das Ergebnis kombiniert die Intelligenz von SAM2 mit der Präzision des Thresholdings", "The result combines the intelligence of SAM2 with the precision of thresholding"],
        ["Stellen Sie zunächst die Thresholding-Werte bei der manuellen SAM-Verfeinerung ein, bevor Sie diese Option verwenden.", "First set the thresholding values in the manual SAM refinement before using this option."],

        ["Die Interpolations-Methode nutzt eine bidirektionale Interpolation zwischen den ausgewählten Support-Masken.", "The interpolation method uses bidirectional interpolation between the selected support masks."],
        ["Diese Methode eignet sich besonders gut für:", "This method is particularly well suited for:"],
        ["Gleichmäßige, sich langsam verändernde Objekte", "Uniform, slowly changing objects"],
        ["Strukturen mit kontinuierlicher Form über mehrere Slices", "Structures with a continuous shape across several slices"],
        ["Fälle, in denen Sie mehrere Support-Masken in verschiedenen Bereichen haben", "Cases where you have multiple support masks in different regions"],
        ["Die Methode interpoliert die Masken zwischen den Support-Punkten und extrapoliert für die Bereiche außerhalb.", "The method interpolates the masks between the support points and extrapolates for the regions outside."],
        ["Empfehlung:", "Recommendation:"],
        ["Wählen Sie Support-Masken, die repräsentativ für verschiedene Bereiche des zu segmentierenden Objekts sind.", "Choose support masks that are representative of different regions of the object to be segmented."],

        ["Die Memory-Attention-Methode nutzt SAM2's natürlichen Cross-Attention-Mechanismus für intelligente Segmentierung.", "The memory-attention method uses SAM2's natural cross-attention mechanism for intelligent segmentation."],
        ["Funktionsweise:", "How it works:"],
        ["Nutzt ALLE Support-Masken in einem gemeinsamen Memory", "Uses ALL support masks in a shared memory"],
        ["SAM2 entscheidet via Attention, welche Referenzen am relevantesten sind", "SAM2 decides via attention which references are most relevant"],
        ["Visuell-basierte Gewichtung statt linearer Interpolation", "Visually based weighting instead of linear interpolation"],
        ["Hochkonfidente Masken werden als neue Memory-Anker verwendet", "High-confidence masks are used as new memory anchors"],
        ["Besonders geeignet für:", "Particularly suitable for:"],
        ["Objekte, die zwischendurch verschwinden und wieder erscheinen", "Objects that disappear and reappear in between"],
        ["Komplexe anatomische Strukturen mit variablen Formen", "Complex anatomical structures with variable shapes"],
        ["Fälle mit mehreren Support-Masken (2-5 empfohlen)", "Cases with multiple support masks (2–5 recommended)"],
        ["Maximale Nutzung von SAM2's Video-Segmentierungs-Architektur", "Maximum use of SAM2's video segmentation architecture"],
        ["Vorteile:", "Advantages:"],
        ["Robuster bei Lücken und näher am ursprünglichen SAM2-Design als andere Methoden.", "More robust with gaps and closer to the original SAM2 design than other methods."],

        ["Die S1SFQ-Methode (Single-Slice Support with Forward Query) ist eine spezialisierte Few-Shot-Segmentierungstechnik.", "The S1SFQ method (Single-Slice Support with Forward Query) is a specialized few-shot segmentation technique."],
        ["Benötigt genau EINE Support-Maske als Referenz", "Requires exactly ONE support mask as reference"],
        ["Segmentiert die restlichen Slices sequentiell von der Mitte nach außen", "Segments the remaining slices sequentially from the center outward"],
        ["Jede neue Segmentierung wird als Kontext für die nächste verwendet", "Each new segmentation is used as context for the next"],
        ["Verbessert die Genauigkeit durch kontinuierliches Lernen", "Improves accuracy through continuous learning"],
        ["Komplexe anatomische Strukturen", "Complex anatomical structures"],
        ["Objekte mit graduellen Formveränderungen", "Objects with gradual shape changes"],
        ["Fälle, in denen eine zentrale Slice gut repräsentativ ist", "Cases where a central slice is well representative"],
        ["Diese Methode stammt aus aktueller Forschung und kann bei vielen medizinischen Segmentierungsaufgaben bessere Ergebnisse als einfache Interpolation liefern.", "This method comes from current research and can deliver better results than simple interpolation for many medical segmentation tasks."],

        ["Der Dice-Koeffizient misst die prozentuale Überlappung zwischen zwei Segmentierungen. Der Wert reicht von 0 (keine Überlappung) bis 1 (perfekte Übereinstimmung).", "The Dice coefficient measures the percentage overlap between two segmentations. The value ranges from 0 (no overlap) to 1 (perfect agreement)."],
        ["Gute Werte:", "Good values:"],
        ["Exzellente Werte:", "Excellent values:"],
        ["Die ASSD misst den durchschnittlichen Abstand zwischen den Oberflächen der beiden 3D-Modelle in Millimetern.", "The ASSD measures the average distance between the surfaces of the two 3D models in millimeters."],
        ["Ein kleinerer Wert ist besser und gibt an, wie genau die Form der Segmentierung der wahren Form entspricht.", "A smaller value is better and indicates how closely the shape of the segmentation matches the true shape."],
        ["Gibt an, wie viel Prozent der Ground-Truth-Maske korrekt erkannt wurden.", "Indicates what percentage of the ground-truth mask was correctly detected."],
        ["Formel:", "Formula:"],
        ["Gibt an, wie viel Prozent der erstellten Segmentierung tatsächlich korrekt ist.", "Indicates what percentage of the created segmentation is actually correct."],
        ["Gibt an, wie viel Prozent des Hintergrunds korrekt als Hintergrund identifiziert wurden.", "Indicates what percentage of the background was correctly identified as background."],
        ["Dies ist die einfache Differenz zwischen dem Volumen der generierten Maske und der Ground-Truth-Maske in Prozent [%].", "This is the simple difference between the volume of the generated mask and the ground-truth mask in percent [%]."],
        ["Ein Wert nahe 0 ist ideal.", "A value close to 0 is ideal."],
        ["Positiver Wert:", "Positive value:"],
        ["Die generierte Maske ist größer.", "The generated mask is larger."],
        ["Negativer Wert:", "Negative value:"],
        ["Die generierte Maske ist kleiner.", "The generated mask is smaller."],

        // ---- JS: Bestätigungs-Modale (statisch) ------------------------------
        ["Achse wechseln?", "Switch axis?"],
        ["Leerer Slice?", "Empty slice?"],
        ["Keine Annotationen vorhanden. Wollen Sie diesen Slice als 'leer' speichern und fortfahren? (Dies zählt als bearbeitet)", "No annotations present. Do you want to save this slice as 'empty' and continue? (This counts as edited)"],
        ["Fensterung zurücksetzen?", "Reset windowing?"],
        ["Fensterung auf die Anfangswerte (wie beim ersten Öffnen) zurücksetzen?", "Reset windowing to the initial values?"],
        ["Annotationen zurücksetzen?", "Reset annotations?"],
        ["Sollen wirklich alle Annotationen für diesen Slice zurückgesetzt werden?", "Do you really want to reset all annotations for this slice?"],
        ["Zurück zur Methodenauswahl?", "Back to method selection?"],
        ["Der gesamte Fortschritt für die SAM-Methode wird zurückgesetzt. Sind Sie sicher?", "All progress for the SAM method will be reset. Are you sure?"],
        ["Bearbeitung abschließen?", "Finish editing?"],
        ["Vorlage anwenden?", "Apply template?"],
        ["Direkt segmentieren?", "Segment directly?"],
        ["Batch-Segmentierung abgeschlossen", "Batch segmentation completed"],
        ["Möchten Sie jetzt die 3D-Maske erstellen?", "Do you want to create the 3D mask now?"],
        ["Restliche Slices überspringen?", "Skip remaining slices?"],
        ["Interpolation abgeschlossen", "Interpolation completed"],
        ["Annotation wiederholen?", "Repeat annotation?"],
        ["Dadurch werden das erstellte 3D-Modell und alle bisherigen Ergebnisse dieser Sitzung gelöscht. Möchten Sie fortfahren?", "This will delete the created 3D model and all previous results of this session. Do you want to continue?"],
        ["Ungewöhnliches Voxel-Spacing erkannt", "Unusual voxel spacing detected"],

        // ---- JS: Alerts (statisch) -------------------------------------------
        ["3D-Viewer ist nicht initialisiert.", "3D viewer is not initialized."],
        ["Die Vergleichs-Ansicht konnte nicht geladen werden.", "The comparison view could not be loaded."],
        ["Ein Server-Fehler ist aufgetreten. Bitte versuchen Sie es erneut.", "A server error occurred. Please try again."],
        ["Ein Netzwerkfehler ist aufgetreten. Bitte überprüfen Sie die Verbindung.", "A network error occurred. Please check the connection."],
        ["Keine Rohdaten für Thresholding verfügbar.", "No raw data available for thresholding."],
        ["Bitte geben Sie einen gültigen Slice-Bereich ein (Von <= Bis).", "Please enter a valid slice range (from <= to)."],
        ["Ungültiger Bereich oder keine Slices vorhanden.", "Invalid range or no slices present."],
        ["Eine Massensegmentierung läuft bereits.", "A batch segmentation is already running."],
        ["Keine SAM-Maske zum Verfeinern verfügbar. Führen Sie zuerst eine SAM-Segmentierung durch.", "No SAM mask available to refine. Run a SAM segmentation first."],
        ["Bitte wählen Sie mindestens eine segmentierte Maske als Beispiel (Support) aus.", "Please select at least one segmented mask as an example (support)."],
        ["Für die S1SFQ-Methode darf genau EINE Support-Maske ausgewählt werden!", "For the S1SFQ method, exactly ONE support mask must be selected!"],
        ["Bitte wählen Sie eine Masken-Datei (PNG) aus.", "Please select a mask file (PNG)."],
        ["Bitte geben Sie eine gültige Slice-Nummer an (>= 1).", "Please enter a valid slice number (>= 1)."],
        ["Fehler beim Lesen der Datei.", "Error reading the file."],
        ["Fehler: Die ausgewählte Datei ist keine gültige JSON-Datei.", "Error: The selected file is not a valid JSON file."],
        ["Bitte erstellen Sie zuerst ein 3D-Modell, bevor Sie eine Ground Truth hochladen.", "Please create a 3D model first before uploading a ground truth."],

        // ---- JS: Status-Meldungen (statisch) ---------------------------------
        ["SAM2-Achsenfixierung aufgehoben. Bitte neue Achse für SAM wählen oder direkt annotieren.", "SAM2 axis fixation released. Please choose a new axis for SAM or annotate directly."],
        ["Bereit.", "Ready."],
        ["Fehler Bilddaten (onerror).", "Image data error (onerror)."],
        ["Keine Slices für diese Achse.", "No slices for this axis."],
        ["Segmentiere aktuellen Slice (SAM)...", "Segmenting current slice (SAM)..."],
        ["Thresholds zurückgesetzt.", "Thresholds reset."],
        ["Annotation (SAM) bereit.", "Annotation (SAM) ready."],
        ["Letzte SAM-Aktion rückgängig.", "Last SAM action undone."],
        ["SAM-Annotationen zurückgesetzt.", "SAM annotations reset."],
        ["Spielmodus: Segmentieren Sie mit SAM.", "Play mode: segment with SAM."],
        ["Segmentierungsmethode wählen.", "Select segmentation method."],
        ["Fensterung anpassen.", "Adjust windowing."],
        ["Thresholding.", "Thresholding."],
        ["SAM: Bitte Achse für Annotation wählen oder direkt annotieren.", "SAM: Please choose an axis for annotation or annotate directly."],
        ["SAM: Bereit für Annotation.", "SAM: Ready for annotation."],
        ["Abgebrochen.", "Cancelled."],
        ["Interner Fehler: Ungültige NIfTI-URL erhalten.", "Internal error: invalid NIfTI URL received."],
        ["3D-Viewer wird initialisiert...", "Initializing 3D viewer..."],
        ["3D-Maske wird geladen...", "Loading 3D mask..."],
        ["3D-Ansicht wird konfiguriert...", "Configuring 3D view..."],
        ["3D-Ansicht bereit! Segmentierung erfolgreich abgeschlossen.", "3D view ready! Segmentation completed successfully."],
        ["Fehler: 3D-Viewer-Komponente konnte nicht geladen werden.", "Error: 3D viewer component could not be loaded."],
        ["Keine SAM-Annotationen zum Speichern als Vorlage.", "No SAM annotations to save as a template."],
        ["Aktuelle Annotationen als Vorlage gespeichert.", "Current annotations saved as a template."],
        ["Vorlage auf aktuellen Slice angewendet.", "Template applied to the current slice."],
        ["Zuerst Vorlage speichern.", "Save a template first."],
        ["Keine Slices für Vorlage ausgewählt.", "No slices selected for the template."],
        ["Alle Slices dieser Achse bereits bearbeitet.", "All slices of this axis are already edited."],
        ["Markiere restliche Slices als leer...", "Marking remaining slices as empty..."],
        ["Hybrid-Parameter für Few-Shot gespeichert.", "Hybrid parameters saved for few-shot."],
        ["Initialisiere Anwendung...", "Initializing application..."],
        ["Lade erstes Bild...", "Loading first image..."],
        ["Keine Slices zum Laden.", "No slices to load."],
        ["Fehler: UI-Elemente fehlen.", "Error: UI elements missing."],
        ["Zurück in der Annotations-Stufe.", "Back in the annotation stage."],
        ["Segmentierung abgeschlossen (ohne 3D-Maske).", "Segmentation completed (without 3D mask)."],
        ["Vergleich: grün = deine Segmentierung, rot = Ground-Truth, gelb = Überlappung.", "Comparison: green = your segmentation, red = ground truth, yellow = overlap."],
        ["Interner Fehler: Ungültige URL für 3D-Ansicht. Bitte die Entwicklerkonsole prüfen.", "Internal error: invalid URL for 3D view. Please check the developer console."],

        // ---- JS: Blocking-Loader (statisch) ----------------------------------
        ["Lade Vergleichs-Ansicht...", "Loading comparison view..."],
        ["Neue Segmentierungs-Session wird vorbereitet...", "Preparing new segmentation session..."],
        ["Few-Shot-Segmentierung läuft... Dieser Vorgang kann einige Zeit dauern.", "Few-shot segmentation running... This may take some time."],
        ["Lade gespeicherte Threshold-Parameter...", "Loading saved threshold parameters..."],
        ["Interpolation läuft, bitte warten...", "Interpolation running, please wait..."],
        ["S1SFQ-Segmentierung läuft, bitte warten...", "S1SFQ segmentation running, please wait..."],
        ["Memory-Attention-Segmentierung läuft, bitte warten...", "Memory-attention segmentation running, please wait..."],
        ["Maske wird hochgeladen und verarbeitet...", "Uploading and processing mask..."],
        ["Vergleiche Modelle und berechne Metriken...", "Comparing models and computing metrics..."],
        ["Ordner wird eingelesen...", "Reading folder..."],
        ["Daten werden hochgeladen und verarbeitet...", "Uploading and processing data..."],

        // GT-Vergleichsmodus (Schnell/Genau)
        ["Vergleichsmodus wählen", "Choose comparison mode"],
        ["Wie soll der Ground-Truth-Vergleich laufen? Schnell ist grob (heruntergerechnete Roll-Suche), Genau nutzt die volle Auflösung.", "How should the ground-truth comparison run? Fast is coarse (downsampled roll search), Accurate uses full resolution."],
        ["Schnell (grob, ~30 s)", "Fast (coarse, ~30 s)"],
        ["Genau (Standard, ~2 min)", "Accurate (default, ~2 min)"],
        ["Schneller Vergleich läuft…", "Fast comparison running…"],
        ["Genauer Vergleich läuft…", "Accurate comparison running…"],

        // interact-Plattform-spezifische Strings (iRODS / dcm4chee / DICOM-SEG / Speicher-Dialog)
        ["Als DICOM-SEG in die Datenbank laden", "Save as DICOM-SEG to the database"],
        ["Datei herunterladen", "Download file"],
        ["Ergebnis herunterladen, in iRODS ablegen oder als DICOM-SEG in die Datenbank schreiben", "Download the result, store it in iRODS, or write it as DICOM-SEG to the database"],
        ["Kein Ordner ausgewählt", "No folder selected"],
        ["Laden Sie Daten vom eigenen Computer hoch, stagen Sie einen Ordner aus iRODS oder importieren Sie eine Serie aus der dcm4chee-Datenbank.", "Upload data from your own computer, stage a folder from iRODS, or import a series from the dcm4chee database."],
        ["Nur innerhalb der InterACT-GUI verfügbar", "Only available inside the InterACT GUI"],
        ["Schließen", "Close"],
        ["Wie soll das Ergebnis dieser Segmentierung gesichert werden?", "How should the result of this segmentation be saved?"],
        ["iRODS-Ordner wählen", "Select iRODS folder"],
        ["Vorlage auf Slices der Achse '", "Apply template to slices of axis '"],

        // interact-Datenquelle-Seite (upload.html): iRODS / dcm4chee / Tabs
        ["Eigener Computer", "Own computer"],
        ["Staging aus iRODS", "Stage from iRODS"],
        ["dcm4chee-Datenbank", "dcm4chee database"],
        ["iRODS und die dcm4chee-Datenbank stehen nur zur Verfügung, wenn diese Seite im InterACT-GUI geöffnet wird – nur dort ist die Anmeldung vorhanden, unter der Ihre Zugriffsrechte geprüft und protokolliert werden.", "iRODS and the dcm4chee database are only available when this page is opened inside the InterACT GUI – only there is the login present under which your access rights are checked and logged."],
        ["Wählen Sie einen Ordner in Ihrem iRODS-Bereich. Der Ordner wird unter Ihrem eigenen iRODS-Konto gelesen und in den Arbeitsbereich dieser Sitzung kopiert. Unterstützt werden DICOM-Serien sowie NIfTI- und NRRD-Dateien.", "Select a folder in your iRODS area. The folder is read under your own iRODS account and copied into this session's workspace. DICOM series as well as NIfTI and NRRD files are supported."],
        ["Suchen Sie eine Studie und importieren Sie daraus eine Serie. Die Abfrage läuft über die angemeldete Sitzung des GUI, so dass Ihre Zugriffe wie im Rest der Plattform protokolliert werden.", "Search for a study and import a series from it. The query runs via the GUI's authenticated session, so your accesses are logged like everywhere else on the platform."],
        ["Aus iRODS laden & Segmentierung starten", "Load from iRODS & start segmentation"],
        ["Suche läuft...", "Searching..."],
        ["Daten werden übernommen und verarbeitet...", "Importing and processing data..."],
        ["Es kamen keine Bilddaten zurück. Der Ordner ist vermutlich leer oder nicht lesbar.", "No image data was returned. The folder is probably empty or not readable."],

        // --- i18n-fix-2026-10: ergänzte Übersetzungen ---
        ["Der Datensatz ist leer.", "The dataset is empty."],
        ["Ordner wird aus iRODS geladen...", "Loading folder from iRODS..."],
        ["Kein Zugriff auf die Datenbank. Bitte im InterACT-GUI neu anmelden.", "No access to the database. Please sign in again in the InterACT GUI."],
        ["Keine Serien in dieser Studie", "No series in this study"],
        ["Serie wird aus der dcm4chee-Datenbank geladen...", "Loading series from the dcm4chee database..."],
        ["Keine Studien zu dieser Suche gefunden.", "No studies found for this search."],
        ["Importieren", "Import"],
        ["Serie", "Series"],
        ["Accession-Nr.", "Accession no."],
        ["z. B. Mustermann", "e.g. Smith"],
        ["z. B. 12345", "e.g. 12345"],
        ["z. B. A0001", "e.g. A0001"],
        ["Keine DICOM-Bilder geladen.", "No DICOM images loaded."],
        ["Keine Slices", "No slices"],
        ["Keine 8-bit Bilddaten (data.image_data) empfangen.", "No 8-bit image data (data.image_data) received."],
        ["Keine Annotationen.", "No annotations."],
        ["NIfTI-Erstellung fehlgeschlagen (keine URL).", "NIfTI creation failed (no URL)."],
        ["Keine Slices für Vorlage auf dieser Achse.", "No slices available for a template on this axis."],
        ["Parameter Gespeichert", "Parameters saved"],
        ["Fehler", "Error"],
        ["Netzwerkfehler", "Network error"],
        ["Unbekannter Grund", "Unknown reason"],
        ["Fensterungs-Parameter fehlen in der JSON-Datei.", "Windowing parameters are missing from the JSON file."],
        ["Methode fehlt in der JSON-Datei.", "Method is missing from the JSON file."],
        ["nicht verfügbar", "not available"],
        ["JSON laden", "Load JSON"],
        ["Was soll übernommen werden?", "What should be applied?"],
        ["Segmentiermethode & Parameter", "Segmentation method & parameters"],
        ["Threshold-Verfeinerung", "Threshold refinement"],
        ["Übernehmen", "Apply"],
        ["Ergebnis speichern…", "Save result…"],
        ["Ergebnis speichern", "Save result"],
        ["Segmentierungsergebnis speichern", "Save segmentation result"],
        ["Alle Ergebnisse dieser Sitzung als ZIP-Archiv: 3D-Modelle, Masken, Overlays und die Segmentierungsparameter.", "All results of this session as a ZIP archive: 3D models, masks, overlays and the segmentation parameters."],
        ["In iRODS speichern", "Save to iRODS"],
        ["Dasselbe ZIP-Archiv in einen Ordner Ihres iRODS-Bereichs legen.", "Put the same ZIP archive into a folder in your iRODS area."],
        ["Die Segmentierung wird als SEG-Objekt zur Originalserie in dcm4chee gespeichert und ist danach im Viewer über den Bildern sichtbar.", "The segmentation is stored in dcm4chee as a SEG object belonging to the original series and is then visible in the viewer on top of the images."],
        ["Zielordner in iRODS wählen", "Select target folder in iRODS"],
        ["iRODS-Ordnerauswahl", "iRODS folder selection"],
        ["Nur innerhalb der InterACT-GUI verfügbar - dort ist die Anmeldung vorhanden, unter der Ihre Zugriffsrechte geprüft und protokolliert werden.", "Only available within the InterACT GUI – that is where you are signed in, so your access rights can be checked and logged."],
        ["Ergebnisse werden geprüft...", "Checking results..."],
        ["Das ZIP-Archiv", "The ZIP archive"],
        ["Die Maske", "The mask"],
        ["Fertig.", "Done."],
        ["Der Auftrag ist fehlgeschlagen.", "The job failed."],
        ["Der Auftrag ist nicht mehr bekannt.", "The job is no longer known."],
        ["Zeitüberschreitung: Der Auftrag läuft noch. Prüfen Sie später, ob die Serie in der Datenbank angekommen ist.", "Timeout: the job is still running. Check later whether the series has arrived in the database."],
        ["Ordnerauswahl abgebrochen - es wurde nichts gespeichert.", "Folder selection cancelled – nothing was saved."],
        ["Ergebnisse werden gepackt...", "Packing results..."],
        ["Archiv wird in iRODS geschrieben...", "Writing archive to iRODS..."],
        ["Unbekannter Fehler.", "Unknown error."],
        ["Segmentierungsergebnis in iRODS gespeichert.", "Segmentation result saved in iRODS."],
        ["Als DICOM-SEG speichern?", "Save as DICOM-SEG?"],
        ["Maske wird für den Export vorbereitet...", "Preparing mask for export..."],
        ["Segmentierung wird in die Datenbank geschrieben...", "Writing segmentation to the database..."],
        ["Die API hat den Auftrag nicht angenommen.", "The API did not accept the job."],
        ["SEG-Objekt wird erstellt und gespeichert...", "Creating and saving SEG object..."],
        ["Segmentierung als DICOM-SEG in der Datenbank gespeichert.", "Segmentation saved as DICOM-SEG in the database."],
        ["Download der Segmentierungsergebnisse gestartet.", "Download of segmentation results started."],
        ["Fensterung kann fortgesetzt werden.", "Windowing can be continued."],
        ["Fehler beim Abrufen der Konfiguration", "Error retrieving the configuration"],
        ["SAM-Masken (Support):", "SAM masks (support):"],
        ["Keine segmentierten Masken vorhanden. Segmentieren Sie zuerst einige Slices mit SAM.", "No segmented masks available. Segment a few slices with SAM first."],
        ["Fehler beim Laden der Masken.", "Error loading masks."],
        ["Interpolations-Segmentierung wird gestartet", "Starting interpolation segmentation"],
        ["S1SFQ-Segmentierung wird gestartet", "Starting S1SFQ segmentation"],
        ["Memory-Attention-Segmentierung wird gestartet", "Starting memory-attention segmentation"],
        ["Die Segmentierung wird jetzt durchgeführt und anschließend das 3D-Modell erzeugt.", "The segmentation is now being run; the 3D model will be created afterwards."],
        ["Die Segmentierung wird jetzt mit SAM2's Memory-Attention durchgeführt und anschließend das 3D-Modell erzeugt.", "The segmentation is now being run with SAM2's memory attention; the 3D model will be created afterwards."],
        ["Dieser Vorgang kann einige Zeit dauern.", "This may take some time."],
        ["Konnte gespeicherte Threshold-Parameter nicht laden. Bitte speichern Sie zuerst welche über die 'Verfeinern'-Funktion.", "Could not load saved threshold parameters. Please save some first using the refine function."],
        ["STL-Ausrichtung", "STL alignment"],
        ["⚠️ STL-Ausrichtung prüfen", "⚠️ Check STL alignment"],
        ["STL-Ausrichtung – bitte prüfen", "STL alignment – please check"],
        ["' anwenden:", "':"],
        ["(min. Größe:", "(min. size:"],
        ["SAM2 ist nicht verfügbar", "SAM2 is not available"],
        ["Slice-Index fehlt", "Slice index missing"],
        ["Ungültiger Slice-Index", "Invalid slice index"],
        ["Kein Volumen geladen", "No volume loaded"],
        ["Konnte Slice nicht extrahieren", "Could not extract slice"],
        ["Konnte Bild für SAM-Eingabe nicht erstellen", "Could not create image for SAM input"],
        ["Kein gültiger Dateityp geladen", "No valid file type loaded"],
        ["Keine Maske gefunden.", "No mask found."],
        ["Keine Masken oder Referenz-Shape gefunden", "No masks or reference shape found"],
        ["Fehlende Threshold-Werte", "Missing threshold values"],
        ["Ungültige Threshold-Werte", "Invalid threshold values"],
        ["Keine Masken erstellt", "No masks created"],
        ["Threshold-Werte fehlen", "Threshold values missing"],
        ["Ungültige Eingabe: Referenz oder Maskendaten fehlen.", "Invalid input: reference or mask data missing."],
        ["Konnte finale 3D uint8 Maskendaten nicht erstellen.", "Could not create final 3D uint8 mask data."],
        ["Keine Referenz-Shape für 2D-Masken-Stapelung.", "No reference shape for stacking 2D masks."]
    ];

    var DICT = {};
    for (var i = 0; i < PAIRS.length; i++) { DICT[PAIRS[i][0]] = PAIRS[i][1]; }

    // ---- Regex-Regeln für dynamische Strings ---------------------------------
    // [regex, ersatz]  – ersatz ist String ($1..) oder Funktion(str)->str
    var RULES = [
        [/^Lade Slice (\d+) \((\w+)\)\.\.\.$/, "Loading slice $1 ($2)..."],
        [/^Slice (\d+) \((\w+)\) geladen\.$/, "Slice $1 ($2) loaded."],
        [/^SAM für Slice (\d+) \((\w+)\) gespeichert\. Segmentierung verfügbar\.$/, "SAM saved for slice $1 ($2). Segmentation available."],
        [/^Keine Annotationen für Slice (\d+) \((\w+)\)\.$/, "No annotations for slice $1 ($2)."],
        [/^Maske nicht gespeichert: (.*)$/, "Mask not saved: $1"],
        [/^Fehler SAM: (.*)$/, "SAM error: $1"],
        [/^Fehler beim Laden des Bildes: (.*)$/, "Error loading image: $1"],
        [/^Aktualisiere Fensterung \(WL: (-?\d+(?:\.\d+)?), WW: (-?\d+(?:\.\d+)?)\)\.\.\.$/, "Updating windowing (WL: $1, WW: $2)..."],
        [/^Modus \(SAM\): (.*)$/, "Mode (SAM): $1"],
        [/^SAM2-Annotationen erfolgen nun für die Achse '(\w+)'\.$/, "SAM2 annotations now apply to axis '$1'."],
        [/^Slices (\d+) bis (\d+) markiert\.$/, "Slices $1 to $2 marked."],
        [/^Fehler 3D Maske \((\w+)\): (.*)$/, "3D mask error ($1): $2"],
        [/^Fehler beim Anzeigen der 3D-Maske: (.*)$/, "Error displaying 3D mask: $1"],
        [/^Fehler beim Abschließen: (.*)$/, "Error finishing: $1"],
        [/^Vorlage auf (\d+) Slices der Achse '(\w+)' angewendet\.$/, "Template applied to $1 slices of axis '$2'."],
        [/^(\d+) von (\d+) Support-Maske\(n\) aus JSON wiederhergestellt\.$/, "$1 of $2 support mask(s) restored from JSON."],
        [/^Aus JSON geladen: (.*)$/, "Loaded from JSON: $1"],
        [/^Einstellungen aus JSON für Methode '(\w+)' geladen\.$/, "Settings loaded from JSON for method '$1'."],
        [/^Erstelle finale 3D-Maske für Achse '(\w+)' \((\w+)\)\. Bitte warten\.\.\.$/, "Creating final 3D mask for axis '$1' ($2). Please wait..."],
        [/^Generiere Support-Maske (\d+) von (\d+) \(Slice (\d+)\)\.\.\.$/, "Generating support mask $1 of $2 (slice $3)..."],
        [/^Starte SAM-Segmentierung für (\d+) Slices\.\.\.$/, "Starting SAM segmentation for $1 slices..."],
        [/^Verarbeite\.\.\. (\d+) \/ (\d+)$/, "Processing... $1 / $2"],
        [/^Fehler bei der Few-Shot-Segmentierung: (.*)$/, "Error during few-shot segmentation: $1"],
        [/^Fehler bei der S1SFQ-Segmentierung: (.*)$/, "Error during S1SFQ segmentation: $1"],
        [/^Fehler bei der Memory-Attention-Segmentierung: (.*)$/, "Error during memory-attention segmentation: $1"],
        [/^Fehler bei der Support-Masken-Generierung:\n([\s\S]*)$/, "Error during support mask generation:\n$1"],
        [/^Fehler beim Hochladen der Maske: (.*)$/, "Error uploading mask: $1"],
        [/^Fehler beim Vergleich: (.*)$/, "Error during comparison: $1"],
        [/^Fehler: Die JSON-Datei konnte nicht verarbeitet werden\.\n([\s\S]*)$/, "Error: The JSON file could not be processed.\n$1"],
        [/^Spacing-Korrektur fehlgeschlagen: (.*)$/, "Spacing correction failed: $1"],
        [/^Ausgewählte Datei: (.*)$/, "Selected file: $1"],
        [/^Fehler: (.*)$/, "Error: $1"],

        // dynamische Bestätigungs-Modale
        [/^Wenn Sie die Achse wechseln, gehen nicht gespeicherte SAM-Annotationen für die Achse '(\w+)' verloren und die Segmentierung für diese Achse wird zurückgesetzt\. Fortfahren\?$/,
            "If you switch the axis, unsaved SAM annotations for axis '$1' will be lost and the segmentation for this axis will be reset. Continue?"],
        [/^Soll die Vorlage auf (\d+) Slices angewendet werden\? Dies überschreibt bestehende Annotationen auf diesen Slices\.$/,
            "Apply the template to $1 slices? This overwrites existing annotations on these slices."],
        [/^Soll für diese (\d+) Slices auch direkt die SAM-Segmentierung durchgeführt werden\? Dies kann einige Zeit dauern\.$/,
            "Should SAM segmentation also be run directly for these $1 slices? This may take some time."],
        [/^Wollen Sie wirklich die (.+) Slices der Achse '(\w+)' als 'ohne Segmentierung \(SAM\)' markieren und abschließen\?$/,
            "Do you really want to mark the $1 slices of axis '$2' as 'without segmentation (SAM)' and finish?"],
        [/^Soll die Bearbeitung für die Methode '(\w+)'( \(Achse: \w+\))? abgeschlossen und eine 3D-Maske erstellt werden\?$/,
            function (s) {
                return s.replace(/^Soll die Bearbeitung für die Methode '(\w+)'( \(Achse: (\w+)\))? abgeschlossen und eine 3D-Maske erstellt werden\?$/,
                    function (_m, meth, axPart, ax) {
                        return "Should editing for method '" + meth + "'" + (axPart ? " (axis: " + ax + ")" : "") + " be finished and a 3D mask created?";
                    });
            }],
        [/^(.*) Jetzt auf (.+) mm korrigieren\? \(empfohlen\)$/, "$1 Correct to $2 mm now? (recommended)"],
        [/^([\s\S]*)\n\nMöchten Sie jetzt die finale 3D-Maske erstellen\?$/, "$1\n\nDo you want to create the final 3D mask now?"],

        // Few-Shot-Zwischentext bei Interpolation
        [/^Few-Shot-Segmentierung läuft\.\.\. Dieser Vorgang kann einige Zeit dauern\.$/, "Few-shot segmentation running... This may take some time."],

        // Upload-Seite (dynamisch)
        [/^Serie konnte nicht geladen werden: (.*)$/, "Series could not be loaded: $1"],
        [/^Netzwerkfehler beim Serienwechsel: (.*)$/, "Network error while switching series: $1"],
        [/^Upload fehlgeschlagen: (.*)$/, "Upload failed: $1"],
        [/^(\d+) Dateien \((\d+) DICOM\)$/, "$1 files ($2 DICOM)"],
        [/^Serie (\d+): ([\s\S]*) \[([^,\]]*), (\d+) Bilder\]$/, "Series $1: $2 [$3, $4 images]"],

        // --- i18n-fix-2026-10: ergänzte Regeln ---
        // Upload-Seite: iRODS / dcm4chee
        [/^Kein Zugriff \(HTTP (\d+)\)\. Bitte im InterACT-GUI neu anmelden\.$/, "No access (HTTP $1). Please sign in again in the InterACT GUI."],
        [/^Laden fehlgeschlagen \(HTTP (\d+)\)\.$/, "Loading failed (HTTP $1)."],
        [/^Laden fehlgeschlagen: ([\s\S]*)$/, "Loading failed: $1"],
        [/^Import fehlgeschlagen: ([\s\S]*)$/, "Import failed: $1"],
        [/^Die Datenbank antwortete mit HTTP (\d+)\.$/, "The database responded with HTTP $1."],
        [/^(\d+) Studie\(n\) gefunden – Studie anklicken, um die Serien zu sehen\.$/, "$1 study/studies found – click a study to see its series."],
        [/^Serie (\d+)( – [\s\S]*)?$/, "Series $1$2"],
        [/^([\s\S]*?)(\d+) Bilder$/, "$1$2 images"],

        // Viewer: Status, Fehler
        [/^HTTP Fehler (\d+)$/, "HTTP error $1"],
        [/^3D Maske "([\s\S]*)" erstellt! Lade 3D Ansicht\.\.\.( Ergebnisse wurden archiviert\.)?$/,
            function (s) {
                var m = s.match(/^3D Maske "([\s\S]*)" erstellt! Lade 3D Ansicht\.\.\.( Ergebnisse wurden archiviert\.)?$/);
                return '3D mask "' + m[1] + '" created! Loading 3D view...' + (m[2] ? ' Results have been archived.' : '');
            }],
        [/^Fehler beim Laden der 3D-Ansicht: ([\s\S]*)$/, "Error loading 3D view: $1"],
        [/^Vorlage auf (\d+) Slices anwenden$/, "Apply template to $1 slices"],
        [/^Serverfehler für Slice (\d+)$/, "Server error for slice $1"],
        [/^Massen-SAM-Segmentierung abgeschlossen\. (\d+) Slices erfolgreich, (\d+) mit Fehlern\.$/, "Batch SAM segmentation finished. $1 slices successful, $2 with errors."],
        [/^Fehler beim Speichern der Parameter: ([\s\S]*)$/, "Error saving parameters: $1"],
        [/^Netzwerkfehler beim Speichern der Parameter: ([\s\S]*)$/, "Network error saving parameters: $1"],
        [/^Voxel-Spacing korrigiert \(Achse (\w+): (\S+) → (\S+) mm\)\.$/, "Voxel spacing corrected (axis $1: $2 → $3 mm)."],
        [/^Fehler beim Neuladen: ([\s\S]*)$/, "Error reloading: $1"],
        [/^Details: ([\s\S]*)$/, "Details: $1"],

        // JSON-Import
        [/^Fensterung WL=(-?\d+), WW=(-?\d+)([\s\S]*)$/, "Windowing WL=$1, WW=$2$3"],
        [/^Unbekannte Methode in JSON: ([\s\S]*)$/, "Unknown method in JSON: $1"],
        [/^SAM – Achse: ([^,]+), (\d+) Slice\(s\) annotiert$/, "SAM – axis: $1, $2 slice(s) annotated"],
        [/^(-?\d+) bis (-?\d+) HU$/, "$1 to $2 HU"],

        // Export (ZIP, iRODS, DICOM-SEG)
        [/^Ergebnisse konnten nicht ermittelt werden: ([\s\S]*)$/, "Results could not be determined: $1"],
        [/^(Das ZIP-Archiv|Die Maske) konnte nicht erstellt werden \(HTTP (\d+)\)\.$/, "$1 could not be created (HTTP $2)."],
        [/^(Das ZIP-Archiv|Die Maske) ist leer\.$/, "$1 is empty."],
        [/^Unerwartete Antwort des GUI \(HTTP (\d+)\)\. Möglicherweise ist Ihre Anmeldung abgelaufen\.$/, "Unexpected response from the GUI (HTTP $1). Your session may have expired."],
        [/^Gespeichert in ([^:]+): ([\s\S]*)$/, "Saved in $1: $2"],
        [/^Speichern in iRODS fehlgeschlagen: ([\s\S]*)$/, "Saving to iRODS failed: $1"],
        [/^Speichern als DICOM-SEG fehlgeschlagen: ([\s\S]*)$/, "Saving as DICOM-SEG failed: $1"],
        [/^Die Segmentierung wird als neue Serie "([\s\S]*?)" zur Originalserie( "([\s\S]*)")? in die dcm4chee-Datenbank geschrieben\. Der Zugriff wird protokolliert\.$/,
            function (s) {
                var m = s.match(/^Die Segmentierung wird als neue Serie "([\s\S]*?)" zur Originalserie( "([\s\S]*)")? in die dcm4chee-Datenbank geschrieben\. Der Zugriff wird protokolliert\.$/);
                return 'The segmentation will be written to the dcm4chee database as a new series "' + m[1]
                    + '" belonging to the original series' + (m[3] ? ' "' + m[3] + '"' : '') + '. This access is logged.';
            }],

        // Few-Shot
        [/^Gesamt: (\d+) Masken \((\d+) SAM, (\d+) Few-Shot\)$/, "Total: $1 masks ($2 SAM, $3 few-shot)"],

        // Server-Meldungen mit Werten
        [/^DICOM-Datei (.+) nicht gefunden$/, "DICOM file $1 not found"],
        [/^SAM für (.+) abgeschlossen\.$/, "SAM finished for $1."],
        [/^Fehler Erstellung Analyse-Maske: ([\s\S]*)$/, "Error creating analysis mask: $1"],
        [/^Threshold-Verfeinerung angewendet \((\d+) Overlay\(s\) aktualisiert\)$/, "Threshold refinement applied ($1 overlay(s) updated)"],
        [/^Fehler NIfTI Objekt: ([\s\S]*)$/, "NIfTI object error: $1"],
        [/^Fehler Speichern NIfTI: ([\s\S]*)$/, "Error saving NIfTI: $1"],
        [/^Fehler Nachbearbeitung: ([\s\S]*)$/, "Post-processing error: $1"],
        [/^Konnte Ref-Shape aus erstem DICOM nicht laden: ([\s\S]*)$/, "Could not load reference shape from first DICOM: $1"]
    ];

    function applyRules(s) {
        for (var r = 0; r < RULES.length; r++) {
            if (RULES[r][0].test(s)) {
                var rep = RULES[r][1];
                if (typeof rep === 'function') return rep(s);
                // i18n-fix-2026-10: eingesetzte Teile ($1, $2, ...) ebenfalls übersetzen,
                // z.B. "Fehler: Kein Volumen geladen" -> "Error: No volume loaded".
                var groups = s.match(RULES[r][0]);
                return rep.replace(/\$(\d)/g, function (_m, d) {
                    var g = groups[+d];
                    return (typeof g === 'string') ? translate(g) : '';
                });
            }
        }
        return null;
    }

    // Übersetzt einen (deutschen) String in die aktive Sprache.
    function translate(de) {
        if (currentLang === 'de') return de;
        if (Object.prototype.hasOwnProperty.call(DICT, de)) return DICT[de];
        // Fallback: interne Umbrüche/Mehrfach-Leerzeichen zusammenfassen (mehrzeilige <p> im Template).
        var norm = de.replace(/\s+/g, ' ').trim();
        if (norm !== de && Object.prototype.hasOwnProperty.call(DICT, norm)) return DICT[norm];
        var r = applyRules(de);
        return (r !== null) ? r : de;
    }

    // ---- Textknoten übersetzen (reversibel, ohne Feedback-Schleife) ----------
    function skipParent(parent) {
        if (!parent) return true;
        var n = parent.nodeName;
        if (n === 'SCRIPT' || n === 'STYLE' || n === 'NOSCRIPT' || n === 'TEXTAREA') return true;
        if (parent.closest && parent.closest('[data-i18n-skip]')) return true;
        return false;
    }

    function applyToNode(node) {
        var parent = node.parentNode;
        if (skipParent(parent)) return;
        var raw = node.nodeValue;
        if (!raw) return;
        var trimmed = raw.trim();
        if (!trimmed) return;

        var de = CANON.get(node);
        if (de === undefined) {
            de = trimmed;
            CANON.set(node, de);
        } else if (trimmed !== de && raw !== LASTW.get(node) && trimmed !== translate(de)) {
            // Die App hat neuen (deutschen) Text gesetzt -> Kanonik aktualisieren.
            de = trimmed;
            CANON.set(node, de);
        }

        var target = (currentLang === 'de') ? de : translate(de);
        if (target === trimmed) return;
        var newRaw = raw.replace(trimmed, target);
        LASTW.set(node, newRaw);
        node.nodeValue = newRaw;
    }

    function applyAttr(el, attr, dsKey) {
        var cur = el.getAttribute(attr);
        var de = el.dataset[dsKey];
        if (de === undefined) {
            if (!cur) return;
            de = cur;
            el.dataset[dsKey] = de;
        }
        var target = (currentLang === 'de') ? de : translate(de);
        if (el.getAttribute(attr) !== target) el.setAttribute(attr, target);
    }

    function walkElement(root) {
        if (root.nodeType === 1 && skipParent(root)) return;
        // Textknoten
        var tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
        var n;
        while ((n = tw.nextNode())) applyToNode(n);
        // Attribute (title / placeholder)
        if (root.querySelectorAll) {
            var titled = root.querySelectorAll('[title]');
            for (var a = 0; a < titled.length; a++) applyAttr(titled[a], 'title', 'i18nTitleDe');
            var ph = root.querySelectorAll('[placeholder]');
            for (var b = 0; b < ph.length; b++) applyAttr(ph[b], 'placeholder', 'i18nPhDe');
            if (root.nodeType === 1 && root.hasAttribute('title')) applyAttr(root, 'title', 'i18nTitleDe');
            if (root.nodeType === 1 && root.hasAttribute('placeholder')) applyAttr(root, 'placeholder', 'i18nPhDe');
        }
    }

    function applyLanguage(lang) {
        currentLang = lang;
        document.documentElement.lang = lang;
        if (TITLE_CANON === undefined) TITLE_CANON = document.title;
        document.title = (lang === 'de') ? TITLE_CANON : translate(TITLE_CANON);
        if (document.body) walkElement(document.body);
        updateSwitcherUI();
    }

    // ---- MutationObserver: dynamisch erzeugte Inhalte übersetzen --------------
    function startObserver() {
        var mo = new MutationObserver(function (muts) {
            for (var i = 0; i < muts.length; i++) {
                var m = muts[i];
                if (m.type === 'characterData') {
                    applyToNode(m.target);
                } else if (m.type === 'childList') {
                    for (var j = 0; j < m.addedNodes.length; j++) {
                        var node = m.addedNodes[j];
                        if (node.nodeType === 3) applyToNode(node);
                        else if (node.nodeType === 1) walkElement(node);
                    }
                    // Attributänderungen an title/placeholder bestehender Elemente
                }
            }
        });
        mo.observe(document.body, { childList: true, subtree: true, characterData: true });
    }

    // ---- window.alert umschließen (Browser-Dialoge sind keine DOM-Knoten) -----
    var _alert = window.alert.bind(window);
    window.alert = function (msg) {
        try { _alert(currentLang === 'de' ? msg : translate(String(msg))); }
        catch (e) { _alert(msg); }
    };

    // ---- Sprachumschalter (oben rechts) --------------------------------------
    var switcherEl = null;
    function updateSwitcherUI() {
        if (!switcherEl) return;
        var btns = switcherEl.querySelectorAll('button[data-lang]');
        for (var i = 0; i < btns.length; i++) {
            btns[i].classList.toggle('active', btns[i].getAttribute('data-lang') === currentLang);
        }
    }
    function buildSwitcher() {
        var style = document.createElement('style');
        style.textContent =
            '#lang-switcher{position:fixed;top:8px;right:10px;z-index:10000;display:flex;gap:2px;' +
            'background:#fff;border:1px solid #c9d3e0;border-radius:6px;padding:2px;box-shadow:0 1px 4px rgba(0,0,0,.15);' +
            'font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}' +
            '#lang-switcher button{border:none;background:transparent;color:#334;cursor:pointer;' +
            'font-size:0.8rem;font-weight:600;padding:3px 9px;border-radius:4px;line-height:1.2;}' +
            '#lang-switcher button:hover{background:#eef3fb;}' +
            '#lang-switcher button.active{background:#1976d2;color:#fff;}';
        document.head.appendChild(style);

        var box = document.createElement('div');
        box.id = 'lang-switcher';
        box.setAttribute('data-i18n-skip', '');
        box.title = 'Sprache / Language';
        ['en', 'de'].forEach(function (lg) {
            var b = document.createElement('button');
            b.type = 'button';
            b.setAttribute('data-lang', lg);
            b.textContent = (lg === 'en') ? 'EN' : 'DE';
            b.addEventListener('click', function () {
                if (lg !== currentLang) applyLanguage(lg);
            });
            box.appendChild(b);
        });
        document.body.appendChild(box);
        switcherEl = box;
    }

    // ---- Init -----------------------------------------------------------------
    // interact-Variante: fest Englisch, KEIN Sprachumschalter (buildSwitcher wird nicht aufgerufen).
    function init() {
        applyLanguage(currentLang);   // Englisch anwenden (kein Umschalter)
        startObserver();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Öffentliche API (optional)
    window.i18n = {
        set: function (lang) { applyLanguage(lang); },
        get: function () { return currentLang; },
        t: function (de) { return translate(de); }
    };
})();
