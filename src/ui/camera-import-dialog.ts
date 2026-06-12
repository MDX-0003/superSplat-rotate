import { BooleanInput, Button, Container, Label, SelectInput } from '@playcanvas/pcui';

import { localize } from './localization';

interface CameraImportResult {
    mode: 'gt' | 'timeline' | 'both';
    remember: boolean;
}

class CameraImportDialog extends Container {
    show: () => Promise<CameraImportResult | null>;
    hide: () => void;
    destroy: () => void;

    constructor(args = {}) {
        args = {
            ...args,
            id: 'camera-import-dialog',
            class: 'settings-dialog',
            hidden: true,
            tabIndex: -1
        };

        super(args);

        const dialog = new Container({ id: 'dialog' });

        // header
        const headerText = new Label({
            id: 'text',
            text: localize('popup.camera-import.header').toUpperCase()
        });
        const header = new Container({ id: 'header' });
        header.append(headerText);

        // mode selection
        const modeLabel = new Label({
            class: 'label',
            text: localize('popup.camera-import.description')
        });
        const modeRow = new Container({ class: 'row' });
        modeRow.append(modeLabel);

        const modeSelect = new SelectInput({
            class: 'select',
            defaultValue: 'gt' as string,
            options: [
                { v: 'gt' as string, t: localize('popup.camera-import.mode.gt') },
                { v: 'timeline' as string, t: localize('popup.camera-import.mode.timeline') },
                { v: 'both' as string, t: localize('popup.camera-import.mode.both') }
            ]
        });

        // remember checkbox
        const rememberLabel = new Label({
            class: 'label',
            text: localize('popup.camera-import.remember')
        });
        const rememberBoolean = new BooleanInput({ class: 'boolean', value: false });
        const rememberRow = new Container({ class: 'row' });
        rememberRow.append(rememberLabel);
        rememberRow.append(rememberBoolean);

        // content
        const content = new Container({ id: 'content' });
        content.append(modeRow);
        content.append(modeSelect);
        content.append(rememberRow);

        // footer
        const footer = new Container({ id: 'footer' });

        const cancelButton = new Button({
            class: 'button',
            text: localize('popup.cancel')
        });

        const importButton = new Button({
            class: 'button',
            text: localize('popup.camera-import.import')
        });

        footer.append(cancelButton);
        footer.append(importButton);

        dialog.append(header);
        dialog.append(content);
        dialog.append(footer);

        this.append(dialog);

        // keyboard and button handling
        let onCancel: () => void;
        let onImport: () => void;

        cancelButton.on('click', () => onCancel());
        importButton.on('click', () => onImport());

        const keydown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                onCancel();
            }
        };

        this.show = () => {
            this.hidden = false;
            document.addEventListener('keydown', keydown);
            this.dom.focus();

            return new Promise<CameraImportResult | null>((resolve) => {
                onCancel = () => {
                    resolve(null);
                };

                onImport = () => {
                    resolve({
                        mode: modeSelect.value as 'gt' | 'timeline' | 'both',
                        remember: rememberBoolean.value
                    });
                };
            }).finally(() => {
                document.removeEventListener('keydown', keydown);
                this.hide();
            });
        };

        this.hide = () => {
            this.hidden = true;
        };

        this.destroy = () => {
            this.hide();
            super.destroy();
        };
    }
}

export { CameraImportDialog, CameraImportResult };
