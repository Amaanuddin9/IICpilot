import * as vscode from 'vscode';
import { Backend, BackendOptions, Frame } from './backend';
import { chatHtml } from './webview';

const VIEW_ID = 'adaptiveIicpilot.chat';

function readOptions(): BackendOptions {
    const config = vscode.workspace.getConfiguration('adaptiveIicpilot');
    const configured = (config.get<string>('repoPath') || '').trim();
    const fallback = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();
    return {
        pythonPath: config.get<string>('pythonPath') || 'python3',
        repoPath: configured.length > 0 ? configured : fallback,
        ollamaModel: config.get<string>('ollamaModel') || 'llama3.1:8b',
        backend: config.get<string>('backend') || 'orfs'
    };
}

class ChatViewProvider implements vscode.WebviewViewProvider {
    private view: vscode.WebviewView | undefined;
    private backend: Backend;
    private status: vscode.StatusBarItem;
    private hitl: boolean;
    private model = '';
    private memory = '';

    constructor(
        private readonly extensionUri: vscode.Uri,
        private readonly output: vscode.OutputChannel
    ) {
        this.hitl = vscode.workspace.getConfiguration('adaptiveIicpilot').get<boolean>('hitl') ?? false;
        this.status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
        this.backend = new Backend(
            output,
            (frame) => this.handleFrame(frame),
            (info) => this.handleExit(info)
        );
    }

    public dispose(): void {
        this.backend.stop();
        this.status.dispose();
    }

    public resolveWebviewView(view: vscode.WebviewView): void {
        this.view = view;
        view.webview.options = { enableScripts: true, localResourceRoots: [this.extensionUri] };
        view.webview.html = chatHtml(view.webview);

        view.webview.onDidReceiveMessage((message) => this.handleWebviewMessage(message));
        view.onDidDispose(() => {
            this.view = undefined;
        });

        this.startBackend();
    }

    public reveal(): void {
        vscode.commands.executeCommand(`${VIEW_ID}.focus`);
    }

    public startBackend(): void {
        const options = readOptions();
        this.post({ type: 'status', text: `Starting backend (${options.pythonPath})…`, busy: true });
        this.backend.start(options);
        // Push the current HITL setting so the backend and UI agree from the start.
        this.backend.send({ type: 'config', hitl: this.hitl });
        this.post({ type: 'hitl', value: this.hitl });
    }

    private handleWebviewMessage(message: any): void {
        switch (message?.type) {
            case 'user': {
                const text = String(message.text ?? '').trim();
                if (text.length === 0) {
                    return;
                }
                if (!this.sendOrWarn({ type: 'user', text })) {
                    return;
                }
                this.post({ type: 'user', text });
                break;
            }
            case 'decision':
                this.sendOrWarn({
                    type: 'decision',
                    id: message.id,
                    decision: message.decision,
                    changes: message.changes
                });
                break;
            case 'hitl':
                this.hitl = Boolean(message.value);
                this.sendOrWarn({ type: 'config', hitl: this.hitl });
                break;
            case 'reset_memory':
                this.sendOrWarn({ type: 'reset_memory' });
                break;
            case 'restart':
                this.post({ type: 'clear_error' });
                this.startBackend();
                break;
            case 'show_logs':
                this.output.show(true);
                break;
        }
    }

    private sendOrWarn(message: object): boolean {
        if (this.backend.send(message)) {
            return true;
        }
        this.post({
            type: 'backend_down',
            text: 'The backend is not running. Restart it to continue.'
        });
        return false;
    }

    private handleFrame(frame: Frame): void {
        if (frame.type === 'ready') {
            this.model = String(frame.model ?? '');
            this.memory = String(frame.memory ?? '');
            this.updateStatus();
        }
        if (frame.type === 'report' && typeof frame.seeded === 'boolean') {
            this.memory = frame.seeded ? 'WARM' : 'COLD';
            this.updateStatus();
        }
        this.post({ type: 'frame', frame });
    }

    private handleExit(info: string): void {
        this.post({ type: 'backend_down', text: info });
        this.status.text = 'IICPilot: stopped';
        this.status.show();
    }

    private updateStatus(): void {
        const parts = [this.model, this.memory].filter((part) => part.length > 0);
        this.status.text = `IICPilot: ${parts.join(' · ')}`;
        this.status.tooltip = 'Adaptive IICPilot backend';
        this.status.command = 'adaptiveIicpilot.openChat';
        this.status.show();
    }

    private post(message: object): void {
        this.view?.webview.postMessage(message);
    }
}

export function activate(context: vscode.ExtensionContext): void {
    const output = vscode.window.createOutputChannel('Adaptive IICPilot');
    const provider = new ChatViewProvider(context.extensionUri, output);

    context.subscriptions.push(
        output,
        provider,
        vscode.window.registerWebviewViewProvider(VIEW_ID, provider, {
            webviewOptions: { retainContextWhenHidden: true }
        }),
        vscode.commands.registerCommand('adaptiveIicpilot.openChat', () => provider.reveal()),
        vscode.commands.registerCommand('adaptiveIicpilot.restartBackend', () => provider.startBackend())
    );
}

export function deactivate(): void {
    // Subscriptions dispose the backend process.
}
