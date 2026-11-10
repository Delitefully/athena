import Foundation

/// The one thing outside the app it calls: athena-rex. Its path is ATHENA_REX_CLI, which `athena-rex menubar run` sets to
/// itself, so tests hand the app a fake and nothing here ever names launchctl, the Rex server or a module's tools.
struct CliResult {
    let code: Int32
    let out: String
    let err: String

    var ok: Bool { code == 0 }

    /// The first thing it said about a failure, for the menu and the alert.
    var problem: String {
        let text = [err, out].map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.first { !$0.isEmpty } ?? ""
        let line = text.split(separator: "\n").first.map(String.init) ?? "exited \(code)"
        return line.count > 200 ? String(line.prefix(199)) + "…" : line
    }
}

final class Cli {
    let path: String
    private(set) var pending = 0  // main thread only

    init() {
        let env = ProcessInfo.processInfo.environment
        path = env["ATHENA_REX_CLI"].flatMap { $0.isEmpty ? nil : $0 } ?? NSHomeDirectory() + "/.local/bin/athena-rex"
    }

    /// Runs off the main thread and answers on it.
    func run(_ args: [String], timeout: TimeInterval = 120, done: @escaping (CliResult) -> Void) {
        pending += 1
        let path = self.path
        DispatchQueue.global().async {
            let result = Cli.exec(path, args, timeout: timeout)
            DispatchQueue.main.async {
                self.pending -= 1
                done(result)
            }
        }
    }

    static func exec(_ path: String, _ args: [String], timeout: TimeInterval) -> CliResult {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = args
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        process.standardInput = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return CliResult(code: -1, out: "", err: "\(path): \(error.localizedDescription)")
        }
        let got = Got()
        let reading = DispatchGroup()
        reading.enter()
        DispatchQueue.global().async { got.set(out: out.fileHandleForReading.readDataToEndOfFile()); reading.leave() }
        reading.enter()
        DispatchQueue.global().async { got.set(err: err.fileHandleForReading.readDataToEndOfFile()); reading.leave() }
        let limit = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: limit)
        process.waitUntilExit()
        limit.cancel()
        _ = reading.wait(timeout: .now() + 5)  // a child it left running may hold the pipe open; its exit is the answer
        let (outData, errData) = got.both()
        return CliResult(code: process.terminationStatus, out: String(decoding: outData, as: UTF8.self),
                         err: String(decoding: errData, as: UTF8.self))
    }
}

private final class Got {
    private let lock = NSLock()
    private var out = Data(), err = Data()

    func set(out data: Data) { lock.lock(); out = data; lock.unlock() }
    func set(err data: Data) { lock.lock(); err = data; lock.unlock() }
    func both() -> (Data, Data) { lock.lock(); defer { lock.unlock() }; return (out, err) }
}
