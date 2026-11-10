import Foundation

// What athena-rex says, as the CLI prints it. Each field the app doesn't need is left out, so a new one never breaks it.

struct ServiceRow: Decodable {
    let name: String
    let state: String
    let pid: Int?
    let every: Int?
    let module: Bool?
}

struct ServicesStatus: Decodable {
    let stopped: Bool
    let services: [ServiceRow]
}

struct WorkerRow: Decodable {
    let name: String
    let state: String?
}

// The modules MENU CONTRACT: one object per module in the config list, loaded or not.
struct ModuleAction: Decodable {
    let id: String
    let label: String
}

struct Module: Decodable {
    let name: String
    let title: String
    let loaded: Bool?
    let state: String
    let detail: String?
    let actions: [ModuleAction]?
}

// `athena-rex doctor --last --json`: the last pass, read from what the doctor job wrote; doctor itself is never run to poll.
struct DoctorCheck: Decodable {
    let name: String
    let ok: Bool
    let detail: String?
}

struct DoctorLast: Decodable {
    let ran: Bool
    let age: Int?
    let checks: [DoctorCheck]
}

enum Icon: String {
    case idle, working, needsYou = "needs-you", stopped
}

/// What the app last heard. A part that could not be read is nil, and the menu says so instead of guessing.
struct Snapshot {
    var services: ServicesStatus?
    var workers: [WorkerRow]?
    var modules: [Module]?
    var doctor: DoctorLast?
    var moduleErrors: [String: String] = [:]

    static let working: Set<String> = ["working", "starting"]
    static let needsYou: Set<String> = ["blocked", "limit", "done", "exited", "idle-unreported"]

    var stopped: Bool { services?.stopped ?? false }

    var icon: Icon {
        if stopped { return .stopped }
        let states = (workers ?? []).compactMap { $0.state }
        if states.contains(where: Snapshot.needsYou.contains) { return .needsYou }
        if states.contains(where: Snapshot.working.contains) { return .working }
        return .idle
    }

    /// The one-line fleet summary at the top of the menu.
    var summary: String {
        if stopped { return "athena is stopped" }
        guard let workers = workers else { return "workers: unavailable" }
        if workers.isEmpty { return "no workers" }
        let working = workers.filter { Snapshot.working.contains($0.state ?? "") }.count
        let needs = workers.filter { Snapshot.needsYou.contains($0.state ?? "") }.count
        let rest = workers.count - working - needs
        var parts: [String] = []
        if working > 0 { parts.append("\(working) working") }
        if needs > 0 { parts.append("\(needs) need\(needs == 1 ? "s" : "") you") }
        if rest > 0 { parts.append("\(rest) idle") }
        return "\(workers.count) worker\(workers.count == 1 ? "" : "s"): " + parts.joined(separator: ", ")
    }

    /// A part's row is a mark and its name (no "running" or "loaded", no pids). Loaded is a job between its runs.
    static func mark(_ row: ServiceRow) -> Mark {
        switch row.state {
        case "running", "loaded": return .ok
        case "stopped", "unknown": return .unsure
        default: return .bad  // not loaded, not installed, exited N
        }
    }

    mutating func replace(_ module: Module) {
        guard var list = modules, let at = list.firstIndex(where: { $0.name == module.name }) else { return }
        list[at] = module
        modules = list
    }
}

/// The menu, as data: built from a snapshot, and turned into an NSMenu (or printed) by the app.
indirect enum Node {
    case separator
    case item(String, Command?, [Node]?)
    case warning(String, [Node])  // a submenu drawn with the warning look
    case part(String, Mark, Command?)  // a part of athena: a coloured mark and its name, enabled only with a command

    static func info(_ title: String) -> Node { .item(title, nil, nil) }
}

/// Green when the part is ok, red when it is not, amber when nobody can say (stopped, or launchd didn't answer).
enum Mark: String {
    case ok = "✓"
    case bad = "✗"
    case unsure = "!"
}

enum Command {
    case run([String])  // a plain command; the app refreshes after it
    case module(String, String)  // modules action <name> <id>
    case quit
    case start
    case doctorFix  // doctor --fix: its exit 1 means problems remain, which the rows then show, not an error
}

/// Doctor's last pass: three minutes (180 s) without a pass is as good as none, since the job runs every minute.
let doctorStale = 180

func doctorNode(_ snap: Snapshot) -> Node {
    let run: Node = .item("Run doctor --fix", snap.stopped ? nil : .doctorFix, nil)
    if snap.stopped {
        return .item("Doctor: stopped", nil, [.info("athena is stopped: Start athena runs it again"), .separator, run])
    }
    guard let doctor = snap.doctor else { return .warning("Doctor: unavailable", [run]) }
    if !doctor.ran { return .warning("Doctor: not run yet", [run]) }
    let rows: [Node] = doctor.checks.map { .info("\($0.ok ? "✓" : "✗") \($0.name): \($0.detail ?? "")") }
    let sub = rows + [.separator, run]
    let failing = doctor.checks.filter { !$0.ok }.count
    if let age = doctor.age, age > doctorStale {
        return .warning("Doctor: last ran \(age / 60) min ago", sub)
    }
    if failing == 0 { return .item("Doctor: all ok", nil, sub) }
    return .warning("Doctor: \(failing) problem\(failing == 1 ? "" : "s")", sub)
}

func menuNodes(_ snap: Snapshot) -> [Node] {
    var nodes: [Node] = [.info(snap.summary), .separator]
    if let services = snap.services {
        // the dash row is the one that does something: it opens the dashboard; stopped stays stopped
        nodes += services.services.filter { $0.module != true }.map {
            .part($0.name, Snapshot.mark($0), $0.name == "dash" && !snap.stopped ? .run(["dash", "open"]) : nil)
        }
    } else {
        nodes.append(.info("Services: unavailable"))
    }
    nodes += [doctorNode(snap), .separator]
    if let modules = snap.modules {
        for module in modules {
            var sub: [Node] = [.info("\(module.state.capitalized): \(module.detail ?? "")")]
            if let error = snap.moduleErrors[module.name] { sub.append(.info(error)) }
            let actions = module.actions ?? []
            if !actions.isEmpty {
                sub.append(.separator)
                sub += actions.map { .item($0.label, .module(module.name, $0.id), nil) }
            }
            nodes.append(.item("\(module.title): \(module.state)", nil, sub))
        }
        if !modules.isEmpty { nodes.append(.separator) }
    } else {
        nodes += [.info("Modules: unavailable"), .separator]
    }
    nodes += [
        // stopped means stopped: the dashboard and HQ would each start part of athena outside launchd
        .item("Open dashboard", snap.stopped ? nil : .run(["dash", "open"]), nil),
        .item("Open HQ", snap.stopped ? nil : .run(["hq", "open"]), nil),
        .item("Restart services", snap.stopped ? nil : .run(["services", "restart"]), nil),
        .separator,
        snap.stopped ? .item("Start athena", .start, nil) : .item("Quit athena", .quit, nil),
    ]
    return nodes
}

func decode<T: Decodable>(_ type: T.Type, _ text: String) -> T? {
    try? JSONDecoder().decode(type, from: Data(text.utf8))
}
