import { BooleanInput, Button, ColorPicker, Container, Label, NumericInput, SelectInput, SliderInput } from '@playcanvas/pcui';
import { Color, Vec3 } from 'playcanvas';

import { Pose } from '../camera-poses';
import { Events } from '../events';
import { ShortcutManager } from '../shortcut-manager';
import { localize, formatTooltipWithShortcut } from './localization';
import { Tooltips } from './tooltips';

class ViewPanel extends Container {
    constructor(events: Events, tooltips: Tooltips, args = {}) {
        args = {
            ...args,
            id: 'view-panel',
            class: 'panel',
            hidden: true
        };

        super(args);

        // stop pointer events bubbling
        ['pointerdown', 'pointerup', 'pointermove', 'wheel', 'dblclick'].forEach((eventName) => {
            this.dom.addEventListener(eventName, (event: Event) => event.stopPropagation());
        });

        // header

        const header = new Container({
            class: 'panel-header'
        });

        const icon = new Label({
            text: '\uE403',
            class: 'panel-header-icon'
        });

        const label = new Label({
            text: localize('panel.view-options'),
            class: 'panel-header-label'
        });

        header.append(icon);
        header.append(label);

        // colors

        const clrRow = new Container({
            class: 'view-panel-row'
        });

        const clrLabel = new Label({
            text: localize('panel.view-options.colors'),
            class: 'view-panel-row-label'
        });

        const clrPickers = new Container({
            class: 'view-panel-row-pickers'
        });

        const bgClrPicker = new ColorPicker({
            class: 'view-panel-row-picker',
            channels: 3,
            value: [0, 0, 0]
        });

        const selectedClrPicker = new ColorPicker({
            class: 'view-panel-row-picker',
            channels: 4,
            value: [0, 0, 0, 1]
        });

        const unselectedClrPicker = new ColorPicker({
            class: 'view-panel-row-picker',
            channels: 4,
            value: [0, 0, 0, 1]
        });

        const lockedClrPicker = new ColorPicker({
            class: 'view-panel-row-picker',
            channels: 4,
            value: [0, 0, 0, 1]
        });

        const toArray = (clr: Color) => {
            return [clr.r, clr.g, clr.b, clr.a];
        };

        events.on('bgClr', (clr: Color) => {
            bgClrPicker.value = toArray(clr);
        });

        events.on('selectedClr', (clr: Color) => {
            selectedClrPicker.value = toArray(clr);
        });

        events.on('unselectedClr', (clr: Color) => {
            unselectedClrPicker.value = toArray(clr);
        });

        events.on('lockedClr', (clr: Color) => {
            lockedClrPicker.value = toArray(clr);
        });

        clrPickers.append(bgClrPicker);
        clrPickers.append(selectedClrPicker);
        clrPickers.append(unselectedClrPicker);
        clrPickers.append(lockedClrPicker);

        clrRow.append(clrLabel);
        clrRow.append(clrPickers);

        // tonemapping

        const tonemappingRow = new Container({
            class: 'view-panel-row'
        });

        const tonemappingLabel = new Label({
            text: localize('panel.view-options.tonemapping'),
            class: 'view-panel-row-label'
        });

        const tonemappingSelection = new SelectInput({
            class: 'view-panel-row-select',
            defaultValue: 'linear',
            options: [
                { v: 'linear', t: localize('panel.view-options.tonemapping.linear') },
                { v: 'neutral', t: localize('panel.view-options.tonemapping.neutral') },
                { v: 'aces', t: localize('panel.view-options.tonemapping.aces') },
                { v: 'aces2', t: localize('panel.view-options.tonemapping.aces2') },
                { v: 'filmic', t: localize('panel.view-options.tonemapping.filmic') },
                { v: 'hejl', t: localize('panel.view-options.tonemapping.hejl') }
            ]
        });

        tonemappingRow.append(tonemappingLabel);
        tonemappingRow.append(tonemappingSelection);

        // camera fov

        const fovRow = new Container({
            class: 'view-panel-row'
        });

        const fovLabel = new Label({
            text: localize('panel.view-options.fov'),
            class: 'view-panel-row-label'
        });

        const fovSlider = new SliderInput({
            class: 'view-panel-row-slider',
            min: 10,
            max: 120,
            precision: 1,
            value: 60
        });

        fovRow.append(fovLabel);
        fovRow.append(fovSlider);

        // gt camera poses

        const gtCameraApplyRow = new Container({
            class: 'view-panel-row'
        });

        const gtCameraApplyLabel = new Label({
            text: localize('panel.view-options.gt-camera'),
            class: 'view-panel-row-label'
        });

        const gtCameraApplyButton = new Button({
            class: 'view-panel-row-button',
            text: localize('panel.view-options.gt-camera.none'),
            enabled: false
        });

        gtCameraApplyRow.append(gtCameraApplyLabel);
        gtCameraApplyRow.append(gtCameraApplyButton);

        const gtCameraSelectRow = new Container({
            class: 'view-panel-row',
            hidden: true
        });

        const gtCameraSelectLabel = new Label({
            text: localize('panel.view-options.gt-camera.pose'),
            class: 'view-panel-row-label'
        });

        const gtCameraSelect = new SelectInput({
            class: 'view-panel-row-select',
            type: 'number',
            defaultValue: -1,
            options: [
                { v: -1, t: localize('panel.view-options.gt-camera.none') }
            ],
            enabled: false
        });

        gtCameraSelectRow.append(gtCameraSelectLabel);
        gtCameraSelectRow.append(gtCameraSelect);

        const gtCameraSliderRow = new Container({
            class: 'view-panel-row',
            hidden: true
        });

        const gtCameraSliderLabel = new Label({
            text: localize('panel.view-options.gt-camera.index'),
            class: 'view-panel-row-label'
        });

        const gtCameraSlider = new SliderInput({
            class: 'view-panel-row-slider',
            min: 0,
            max: 0,
            sliderMin: 0,
            sliderMax: 0,
            precision: 0,
            step: 1,
            value: 0,
            enabled: false
        });

        gtCameraSliderRow.append(gtCameraSliderLabel);
        gtCameraSliderRow.append(gtCameraSlider);

        // gt camera export

        const gtCameraExportRow = new Container({
            class: 'view-panel-row'
        });

        const gtCameraExportLabel = new Label({
            text: localize('panel.view-options.gt-camera.export'),
            class: 'view-panel-row-label'
        });

        const gtCameraExportButton = new Button({
            class: 'view-panel-row-button',
            text: localize('panel.view-options.gt-camera.export-all'),
            enabled: false
        });

        gtCameraExportRow.append(gtCameraExportLabel);
        gtCameraExportRow.append(gtCameraExportButton);

        const gtCameraExportResRow = new Container({
            class: 'view-panel-row',
            hidden: true
        });

        const gtCameraExportResLabel = new Label({
            text: localize('panel.view-options.gt-camera.export-resolution'),
            class: 'view-panel-row-label'
        });

        const gtCameraExportResSelect = new SelectInput({
            class: 'view-panel-row-select',
            defaultValue: 'HD',
            options: [
                { v: 'viewport', t: localize('popup.render-image.resolution-current') },
                { v: 'HD', t: 'HD (1920×1080)' },
                { v: 'QHD', t: 'QHD (2560×1440)' },
                { v: '4K', t: '4K (3840×2160)' },
                { v: 'custom', t: localize('popup.render-image.resolution-custom') }
            ]
        });

        gtCameraExportResRow.append(gtCameraExportResLabel);
        gtCameraExportResRow.append(gtCameraExportResSelect);

        const gtCameraExportCustomRow = new Container({
            class: 'view-panel-row',
            hidden: true
        });

        const gtCameraExportWidthLabel = new Label({
            text: localize('panel.view-options.gt-camera.export-width'),
            class: 'view-panel-row-label'
        });

        const gtCameraExportWidth = new NumericInput({
            class: 'view-panel-row-input',
            min: 4,
            max: 16000,
            precision: 0,
            value: 1920
        });

        const gtCameraExportHeightLabel = new Label({
            text: localize('panel.view-options.gt-camera.export-height'),
            class: 'view-panel-row-label'
        });

        const gtCameraExportHeight = new NumericInput({
            class: 'view-panel-row-input',
            min: 4,
            max: 16000,
            precision: 0,
            value: 1080
        });

        gtCameraExportCustomRow.append(gtCameraExportWidthLabel);
        gtCameraExportCustomRow.append(gtCameraExportWidth);
        gtCameraExportCustomRow.append(gtCameraExportHeightLabel);
        gtCameraExportCustomRow.append(gtCameraExportHeight);

        // max pose index for export

        const gtCameraExportMaxPosesRow = new Container({
            class: 'view-panel-row'
        });

        const gtCameraExportMaxPosesLabel = new Label({
            text: localize('panel.view-options.gt-camera.export-max-poses'),
            class: 'view-panel-row-label'
        });

        const gtCameraExportMaxPoses = new NumericInput({
            class: 'view-panel-row-input',
            min: 0,
            max: 9999,
            precision: 0,
            value: 44
        });

        gtCameraExportMaxPosesRow.append(gtCameraExportMaxPosesLabel);
        gtCameraExportMaxPosesRow.append(gtCameraExportMaxPoses);

        // circle center display

        const gtCameraExportCenterRow = new Container({
            class: 'view-panel-row'
        });

        const gtCameraExportCenterLabel = new Label({
            text: localize('panel.view-options.gt-camera.export-center'),
            class: 'view-panel-row-label'
        });

        const gtCameraExportCenterValue = new Label({
            class: 'view-panel-row-label',
            text: '—'
        });

        gtCameraExportCenterRow.append(gtCameraExportCenterLabel);
        gtCameraExportCenterRow.append(gtCameraExportCenterValue);

        // offset mode

        const gtCameraExportOffsetModeRow = new Container({
            class: 'view-panel-row'
        });

        const gtCameraExportOffsetModeLabel = new Label({
            text: localize('panel.view-options.gt-camera.export-offset-mode'),
            class: 'view-panel-row-label'
        });

        const gtCameraExportOffsetMode = new SelectInput({
            class: 'view-panel-row-select',
            defaultValue: 'towardCenter',
            options: [
                { v: 'towardCenter', t: localize('panel.view-options.gt-camera.export-offset-center') },
                { v: 'alongForward', t: localize('panel.view-options.gt-camera.export-offset-forward') }
            ]
        });

        gtCameraExportOffsetModeRow.append(gtCameraExportOffsetModeLabel);
        gtCameraExportOffsetModeRow.append(gtCameraExportOffsetMode);

        // offset distance

        const gtCameraExportOffsetRow = new Container({
            class: 'view-panel-row'
        });

        const gtCameraExportOffsetLabel = new Label({
            text: localize('panel.view-options.gt-camera.export-offset'),
            class: 'view-panel-row-label'
        });

        const gtCameraExportOffset = new NumericInput({
            class: 'view-panel-row-input',
            step: 0.01,
            precision: 2,
            value: 0
        });

        gtCameraExportOffsetRow.append(gtCameraExportOffsetLabel);
        gtCameraExportOffsetRow.append(gtCameraExportOffset);

        // sh bands
        const shBandsRow = new Container({
            class: 'view-panel-row'
        });

        const shBandsLabel = new Label({
            text: localize('panel.view-options.sh-bands'),
            class: 'view-panel-row-label'
        });

        const shBandsSlider = new SliderInput({
            class: 'view-panel-row-slider',
            min: 0,
            max: 3,
            precision: 0,
            value: 3
        });

        shBandsRow.append(shBandsLabel);
        shBandsRow.append(shBandsSlider);

        // camera fly speed

        const cameraFlySpeedRow = new Container({
            class: 'view-panel-row'
        });

        const cameraFlySpeedLabel = new Label({
            text: localize('panel.view-options.fly-speed'),
            class: 'view-panel-row-label'
        });

        const cameraFlySpeedSlider = new SliderInput({
            class: 'view-panel-row-slider',
            min: 0.1,
            max: 30,
            precision: 1,
            value: 1
        });

        cameraFlySpeedRow.append(cameraFlySpeedLabel);
        cameraFlySpeedRow.append(cameraFlySpeedSlider);

        // centers size

        const centersSizeRow = new Container({
            class: 'view-panel-row'
        });

        const centersSizeLabel = new Label({
            text: localize('panel.view-options.centers-size'),
            class: 'view-panel-row-label'
        });

        const centersSizeSlider = new SliderInput({
            class: 'view-panel-row-slider',
            min: 0,
            max: 10,
            precision: 1,
            value: 2
        });

        centersSizeRow.append(centersSizeLabel);
        centersSizeRow.append(centersSizeSlider);

        // centers gaussian color
        const centersColorRow = new Container({
            class: 'view-panel-row'
        });

        const centersColorLabel = new Label({
            text: localize('panel.view-options.centers-gaussian-color'),
            class: 'view-panel-row-label'
        });

        const centersColorToggle = new BooleanInput({
            type: 'toggle',
            class: 'view-panel-row-toggle',
            value: false
        });

        centersColorRow.append(centersColorLabel);
        centersColorRow.append(centersColorToggle);

        // outline selection

        const outlineSelectionRow = new Container({
            class: 'view-panel-row'
        });

        const outlineSelectionLabel = new Label({
            text: localize('panel.view-options.outline-selection'),
            class: 'view-panel-row-label'
        });

        const outlineSelectionToggle = new BooleanInput({
            type: 'toggle',
            class: 'view-panel-row-toggle',
            value: false
        });

        outlineSelectionRow.append(outlineSelectionLabel);
        outlineSelectionRow.append(outlineSelectionToggle);

        // show grid

        const showGridRow = new Container({
            class: 'view-panel-row'
        });

        const showGridLabel = new Label({
            text: localize('panel.view-options.show-grid'),
            class: 'view-panel-row-label'
        });

        const showGridToggle = new BooleanInput({
            type: 'toggle',
            class: 'view-panel-row-toggle',
            value: true
        });

        showGridRow.append(showGridLabel);
        showGridRow.append(showGridToggle);

        // show bound

        const showBoundRow = new Container({
            class: 'view-panel-row'
        });

        const showBoundLabel = new Label({
            text: localize('panel.view-options.show-bound'),
            class: 'view-panel-row-label'
        });

        const showBoundToggle = new BooleanInput({
            type: 'toggle',
            class: 'view-panel-row-toggle',
            value: true
        });

        showBoundRow.append(showBoundLabel);
        showBoundRow.append(showBoundToggle);

        // show dimensions

        const showBoundDimensionsRow = new Container({
            class: 'view-panel-row'
        });

        const showBoundDimensionsLabel = new Label({
            text: localize('panel.view-options.show-bound-dimensions'),
            class: 'view-panel-row-label'
        });

        const showBoundDimensionsToggle = new BooleanInput({
            type: 'toggle',
            class: 'view-panel-row-toggle',
            value: false
        });

        showBoundDimensionsRow.append(showBoundDimensionsLabel);
        showBoundDimensionsRow.append(showBoundDimensionsToggle);

        // show camera poses

        const showCameraPosesRow = new Container({
            class: 'view-panel-row'
        });

        const showCameraPosesLabel = new Label({
            text: localize('panel.view-options.show-camera-poses'),
            class: 'view-panel-row-label'
        });

        const showCameraPosesToggle = new BooleanInput({
            type: 'toggle',
            class: 'view-panel-row-toggle',
            value: false
        });

        showCameraPosesRow.append(showCameraPosesLabel);
        showCameraPosesRow.append(showCameraPosesToggle);

        this.append(header);
        this.append(clrRow);
        this.append(tonemappingRow);
        this.append(fovRow);
        this.append(gtCameraApplyRow);
        this.append(gtCameraSelectRow);
        this.append(gtCameraSliderRow);
        this.append(gtCameraExportRow);
        this.append(gtCameraExportResRow);
        this.append(gtCameraExportCustomRow);
        this.append(gtCameraExportMaxPosesRow);
        this.append(gtCameraExportCenterRow);
        this.append(gtCameraExportOffsetModeRow);
        this.append(gtCameraExportOffsetRow);
        this.append(shBandsRow);
        this.append(cameraFlySpeedRow);
        this.append(centersSizeRow);
        this.append(centersColorRow);
        this.append(outlineSelectionRow);
        this.append(showGridRow);
        this.append(showBoundRow);
        this.append(showBoundDimensionsRow);
        this.append(showCameraPosesRow);

        // handle panel visibility

        const setVisible = (visible: boolean) => {
            if (visible === this.hidden) {
                this.hidden = !visible;
                events.fire('viewPanel.visible', visible);
            }
        };

        events.function('viewPanel.visible', () => {
            return !this.hidden;
        });

        events.on('viewPanel.setVisible', (visible: boolean) => {
            setVisible(visible);
        });

        events.on('viewPanel.toggleVisible', () => {
            setVisible(this.hidden);
        });

        events.on('gtCameraPanel.open', () => {
            setVisible(true);
            gtCameraApplyRow.dom.scrollIntoView({ block: 'nearest' });
            gtCameraApplyRow.dom.classList.add('gt-camera-panel-focus');
            window.setTimeout(() => {
                gtCameraApplyRow.dom.classList.remove('gt-camera-panel-focus');
            }, 900);
        });

        events.on('colorPanel.visible', (visible: boolean) => {
            if (visible) {
                setVisible(false);
            }
        });

        // sh bands

        events.on('view.bands', (bands: number) => {
            shBandsSlider.value = bands;
        });

        shBandsSlider.on('change', (value: number) => {
            events.fire('view.setBands', value);
        });

        // splat size

        events.on('camera.splatSize', (value: number) => {
            centersSizeSlider.value = value;
        });

        centersSizeSlider.on('change', (value: number) => {
            events.fire('camera.setSplatSize', value);
            events.fire('camera.setOverlay', true);
            events.fire('camera.setMode', 'centers');
        });

        // centers gaussian color
        events.on('view.centersUseGaussianColor', (value: boolean) => {
            centersColorToggle.value = value;
        });

        centersColorToggle.on('change', (value: boolean) => {
            events.fire('view.setCentersUseGaussianColor', value);
        });

        // camera speed

        events.on('camera.flySpeed', (value: number) => {
            cameraFlySpeedSlider.value = value;
        });

        cameraFlySpeedSlider.on('change', (value: number) => {
            events.fire('camera.setFlySpeed', value);
        });

        // outline selection

        events.on('view.outlineSelection', (value: boolean) => {
            outlineSelectionToggle.value = value;
        });

        outlineSelectionToggle.on('change', (value: boolean) => {
            events.fire('view.setOutlineSelection', value);
        });

        // show grid

        events.on('grid.visible', (visible: boolean) => {
            showGridToggle.value = visible;
        });

        showGridToggle.on('change', () => {
            events.fire('grid.setVisible', showGridToggle.value);
        });

        // show bound

        events.on('camera.bound', (visible: boolean) => {
            showBoundToggle.value = visible;
        });

        showBoundToggle.on('change', () => {
            events.fire('camera.setBound', showBoundToggle.value);
        });

        // show dimensions

        events.on('camera.boundDimensions', (visible: boolean) => {
            showBoundDimensionsToggle.value = visible;
        });

        showBoundDimensionsToggle.on('change', () => {
            events.fire('camera.setBoundDimensions', showBoundDimensionsToggle.value);
        });

        // show camera poses

        events.on('camera.showPoses', (visible: boolean) => {
            showCameraPosesToggle.value = visible;
        });

        showCameraPosesToggle.on('change', () => {
            events.fire('camera.setShowPoses', showCameraPosesToggle.value);
        });

        // background color

        bgClrPicker.on('change', (value: number[]) => {
            events.fire('setBgClr', new Color(value[0], value[1], value[2]));
        });

        selectedClrPicker.on('change', (value: number[]) => {
            events.fire('setSelectedClr', new Color(value[0], value[1], value[2], value[3]));
        });

        unselectedClrPicker.on('change', (value: number[]) => {
            events.fire('setUnselectedClr', new Color(value[0], value[1], value[2], value[3]));
        });

        lockedClrPicker.on('change', (value: number[]) => {
            events.fire('setLockedClr', new Color(value[0], value[1], value[2], value[3]));
        });

        // camera fov

        events.on('camera.fov', (fov: number) => {
            fovSlider.value = fov;
        });

        fovSlider.on('change', (value: number) => {
            events.fire('camera.setFov', value);
        });

        // gt camera poses

        let gtCameraPoses: Pose[] = [];
        let gtCameraIndex = 0;
        let updatingGtCameraControls = false;

        const gtCameraLabel = (pose: Pose, index: number) => {
            return `${index}: ${pose.name}`;
        };

        const syncGtCameraControls = () => {
            const hasGtCameras = gtCameraPoses.length > 0;
            const maxIndex = Math.max(0, gtCameraPoses.length - 1);

            gtCameraIndex = Math.min(gtCameraIndex, maxIndex);

            updatingGtCameraControls = true;

            gtCameraApplyButton.enabled = hasGtCameras;
            gtCameraApplyButton.text = hasGtCameras ?
                localize('panel.view-options.gt-camera.apply') :
                localize('panel.view-options.gt-camera.none');

            gtCameraSelectRow.hidden = !hasGtCameras;
            gtCameraSliderRow.hidden = !hasGtCameras;
            gtCameraSelect.enabled = hasGtCameras;
            gtCameraSlider.enabled = hasGtCameras;

            gtCameraSelect.options = hasGtCameras ?
                gtCameraPoses.map((pose, index) => ({ v: index, t: gtCameraLabel(pose, index) })) :
                [{ v: -1, t: localize('panel.view-options.gt-camera.none') }];
            gtCameraSelect.value = hasGtCameras ? gtCameraIndex : -1;

            gtCameraSlider.min = 0;
            gtCameraSlider.max = maxIndex;
            gtCameraSlider.sliderMin = 0;
            gtCameraSlider.sliderMax = maxIndex;
            gtCameraSlider.value = gtCameraIndex;

            gtCameraExportButton.enabled = hasGtCameras;
            gtCameraExportResRow.hidden = !hasGtCameras;
            gtCameraExportCustomRow.hidden = !hasGtCameras || gtCameraExportResSelect.value !== 'custom';
            gtCameraExportMaxPosesRow.hidden = !hasGtCameras;
            gtCameraExportCenterRow.hidden = !hasGtCameras;
            gtCameraExportOffsetModeRow.hidden = !hasGtCameras;
            gtCameraExportOffsetRow.hidden = !hasGtCameras;

            // clamp max poses to available range
            if (gtCameraExportMaxPoses.value > maxIndex) {
                gtCameraExportMaxPoses.value = maxIndex;
            }
            gtCameraExportMaxPoses.max = maxIndex;

            // update circle center
            updateCircleCenter();

            updatingGtCameraControls = false;
        };

        const refreshGtCameraPoses = () => {
            const poses = (events.invoke('camera.importedPoses') as readonly Pose[] ?? []);
            gtCameraPoses = poses
            .filter(pose => !!pose.rotation && !!pose.intrinsics)
            .slice()
            .sort((a, b) => a.frame - b.frame);

            const frame = events.invoke('timeline.frame');
            const frameIndex = gtCameraPoses.findIndex(pose => pose.frame === frame);
            if (frameIndex !== -1) {
                gtCameraIndex = frameIndex;
            }

            syncGtCameraControls();
        };

        const applyPoseOffset = (pose: Pose) => {
            const offset = gtCameraExportOffset.value;
            if (offset === 0) return pose;

            const end = Math.min(gtCameraExportMaxPoses.value + 1, gtCameraPoses.length);
            const c = calculateCircleCenter(gtCameraPoses.slice(0, end));
            const dir = new Vec3();

            if (gtCameraExportOffsetMode.value === 'towardCenter') {
                dir.set(c.x - pose.position.x, c.y - pose.position.y, c.z - pose.position.z).normalize();
            } else {
                const rot = pose.rotation;
                if (rot) {
                    dir.set(0, 0, -1);
                    rot.transformVector(dir, dir);
                }
            }
            dir.mulScalar(offset);

            return {
                ...pose,
                position: pose.position.clone().add(dir),
                target: pose.target.clone().add(dir)
            };
        };

        const setGtCameraIndex = (index: number, apply: boolean) => {
            if (gtCameraPoses.length === 0) {
                syncGtCameraControls();
                return;
            }

            gtCameraIndex = Math.max(0, Math.min(gtCameraPoses.length - 1, Math.round(index)));
            syncGtCameraControls();

            if (apply) {
                const pose = applyPoseOffset(gtCameraPoses[gtCameraIndex]);
                events.fire('camera.setPose', pose, 0);
            }
        };

        gtCameraApplyButton.on('click', () => {
            setGtCameraIndex(gtCameraIndex, true);
        });

        gtCameraSelect.on('change', (value: number) => {
            if (!updatingGtCameraControls) {
                setGtCameraIndex(Number(value), true);
            }
        });

        gtCameraSlider.on('change', (value: number) => {
            if (!updatingGtCameraControls) {
                setGtCameraIndex(value, true);
            }
        });

        const calculateCircleCenter = (poses: readonly Pose[]) => {
            let cx = 0, cy = 0, cz = 0;
            for (const p of poses) {
                cx += p.position.x;
                cy += p.position.y;
                cz += p.position.z;
            }
            const n = poses.length || 1;
            return { x: +(cx / n).toFixed(4), y: +(cy / n).toFixed(4), z: +(cz / n).toFixed(4) };
        };

        const updateCircleCenter = () => {
            const end = Math.min(gtCameraExportMaxPoses.value + 1, gtCameraPoses.length);
            const subset = gtCameraPoses.slice(0, end);
            if (subset.length > 0) {
                const c = calculateCircleCenter(subset);
                gtCameraExportCenterValue.text = `${c.x}, ${c.y}, ${c.z}`;
            } else {
                gtCameraExportCenterValue.text = '—';
            }
        };

        const getExportResolution = () => {
            const presets: Record<string, { width: number, height: number }> = {
                'viewport': events.invoke('targetSize') as { width: number, height: number },
                'HD': { width: 1920, height: 1080 },
                'QHD': { width: 2560, height: 1440 },
                '4K': { width: 3840, height: 2160 }
            };
            if (gtCameraExportResSelect.value === 'custom') {
                return { width: gtCameraExportWidth.value, height: gtCameraExportHeight.value };
            }
            return presets[gtCameraExportResSelect.value] ?? presets['HD'];
        };

        gtCameraExportResSelect.on('change', (value: string) => {
            gtCameraExportCustomRow.hidden = value !== 'custom';
        });

        gtCameraExportMaxPoses.on('change', () => {
            updateCircleCenter();
        });

        gtCameraExportButton.on('click', async () => {
            const { width, height } = getExportResolution();
            const maxIndex = gtCameraExportMaxPoses.value;
            const exportPoses = gtCameraPoses
                .slice(0, maxIndex + 1)
                .map(p => applyPoseOffset(p));
            await events.invoke('render.batchGtCameras', exportPoses, width, height);
        });

        events.on('timeline.frame', (frame: number) => {
            const frameIndex = gtCameraPoses.findIndex(pose => pose.frame === frame);
            if (frameIndex !== -1) {
                setGtCameraIndex(frameIndex, false);
            }
        });

        events.on('camera.importedPosesChanged', () => {
            refreshGtCameraPoses();
        });

        refreshGtCameraPoses();

        // tonemapping

        events.on('camera.tonemapping', (tonemapping: string) => {
            tonemappingSelection.value = tonemapping;
        });

        tonemappingSelection.on('change', (value: string) => {
            events.fire('camera.setTonemapping', value);
        });

        // tooltips
        const shortcutManager: ShortcutManager = events.invoke('shortcutManager');
        const shortcut = shortcutManager.formatShortcut('grid.toggleVisible');
        tooltips.register(showGridLabel, formatTooltipWithShortcut(localize('panel.view-options.show-grid'), shortcut), 'left');
        tooltips.register(bgClrPicker, localize('panel.view-options.background-color'), 'left');
        tooltips.register(selectedClrPicker, localize('panel.view-options.selected-color'), 'top');
        tooltips.register(unselectedClrPicker, localize('panel.view-options.unselected-color'), 'top');
        tooltips.register(lockedClrPicker, localize('panel.view-options.locked-color'), 'top');
    }
}

export { ViewPanel };
