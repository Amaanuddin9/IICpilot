import * as vscode from 'vscode';
import { ChildProcessWithoutNullStreams, spawn } from 'child_process';

/** One protocol frame from `python -m adaptive_iicpilot.chat --jsonl`. */
export interface Frame {
    type: string;
    [key: string]: any;
}

export interface BackendOptions {
    pythonPath: string;
    repoPath: string;
    ollamaModel: string;
    backend: string;
}

/**
 * Owns the backend child process and turns its stdout into frames.
 *
 * stdout is parsed strictly line-by-line: the backend guarantees one JSON
 * object per line and sends everything else to stderr, which is mirrored into
 * an output channel rather than the chat panel.
 */
export class Backend {
    private proc: ChildProcessWithoutNullStreams | undefined;
    private stdoutBuffer = '';

    constructor(
        private readonly output: vscode.OutputChannel,
        private readonly onFrame: (frame: Frame) => void,
        private readonly onExit: (info: string) => void
    ) {}

    public get running(): boolean {
        return this.proc !== undefined && this.proc.exitCode === null;
    }

    public start(options: BackendOptions): void {
        this.stop();

        const args = ['-m', 'adaptive_iicpilot.chat', '--jsonl'];
        this.output.appendLine(
            `[extension] spawning: ${options.pythonPath} ${args.join(' ')} (cwd=${options.repoPath})`
        );

        try {
            this.proc = spawn(options.pythonPath, args, {
                cwd: options.repoPath,
                env: {
                    ...process.env,
                    OLLAMA_MODEL: options.ollamaModel,
                    ADAPTIVE_IICPILOT_BACKEND: options.backend,
                    PYTHONUNBUFFERED: '1'
                }
            });
        } catch (err) {
            this.onExit(`Failed to spawn ${options.pythonPath}: ${err}`);
            return;
        }

        this.proc.stdout.setEncoding('utf8');
        this.proc.stdout.on('data', (chunk: string) => this.consume(chunk));

        this.proc.stderr.setEncoding('utf8');
        this.proc.stderr.on('data', (chunk: string) => {
            this.output.append(chunk);
        });

        this.proc.on('error', (err) => {
            this.output.appendLine(`[extension] spawn error: ${err.message}`);
            this.onExit(`Could not start the backend: ${err.message}`);
        });

        this.proc.on('exit', (code, signal) => {
            const how = signal ? `signal ${signal}` : `exit code ${code}`;
            this.output.appendLine(`[extension] backend stopped (${how})`);
            this.proc = undefined;
            this.onExit(`Backend stopped (${how}).`);
        });
    }

    /** Line-buffered: a frame may arrive split across chunks. */
    private consume(chunk: string): void {
        this.stdoutBuffer += chunk;
        let newline = this.stdoutBuffer.indexOf('\n');
        while (newline >= 0) {
            const line = this.stdoutBuffer.slice(0, newline).trim();
            this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
            if (line.length > 0) {
                try {
                    this.onFrame(JSON.parse(line) as Frame);
                } catch {
                    // Not protocol output; keep it visible without breaking the panel.
                    this.output.appendLine(`[stdout non-JSON] ${line}`);
                }
            }
            newline = this.stdoutBuffer.indexOf('\n');
        }
    }

    public send(message: object): boolean {
        if (!this.proc || this.proc.stdin.destroyed) {
            return false;
        }
        this.proc.stdin.write(JSON.stringify(message) + '\n');
        return true;
    }

    public stop(): void {
        if (this.proc) {
            this.proc.removeAllListeners('exit');
            this.proc.kill();
            this.proc = undefined;
        }
        this.stdoutBuffer = '';
    }
}
