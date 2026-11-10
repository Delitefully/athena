import AppKit

/// The menu and what its items do. Everything it knows comes from athena-rex and everything it does goes through it:
/// `services status --json`, `status --json` and `modules --json` to read, `services stop|start|restart`, `dash open`,
/// `hq open` and `modules action <name> <id>` to act. Headless (tests, `--dump-menu`, `--click`) it never makes a status item.
final class Controller: NSObject, NSMenuDelegate {
    let cli = Cli()
    let knot = Knot()
    let menu = NSMenu()
    let headless: Bool
    private(set) var snapshot = Snapshot()
    private var item: NSStatusItem?
    private var animation: Timer?
    private var busy = Set<String>()
    private var refreshing = 0
    private var again = false
    private var doctorReadAt = Date.distantPast
    static let doctorEvery: TimeInterval = 30  // its job writes once a minute: reading it every poll would only cost
    private(set) var polls = 0
    private var open = false  // the menu is on screen: it is rebuilt only once it closes, so nothing moves under the pointer
    private var stale = false

    init(headless: Bool) {
        self.headless = headless
        super.init()
        menu.autoenablesItems = false
        menu.delegate = self
    }

    // MARK: reading

    /// One poll at a time: asked while one runs, it runs once more when that one answers. A slow athena-rex never gets a
    /// pile of python processes from the 5 s timer, and an action's own refresh is never lost.
    func refresh() {
        if refreshing > 0 {
            again = true
            return
        }
        let cli = self.cli
        var services: CliResult?, workers: CliResult?, modules: CliResult?, doctor: CliResult?
        let group = DispatchGroup()
        refreshing += 1
        var reads: [([String], (CliResult) -> Void)] = [
            (["services", "status", "--json"], { services = $0 }), (["status", "--json"], { workers = $0 }),
            (["modules", "--json"], { modules = $0 })]
        let readDoctor = Date().timeIntervalSince(doctorReadAt) >= Controller.doctorEvery
        if readDoctor {
            doctorReadAt = Date()
            reads.append((["doctor", "--last", "--json"], { doctor = $0 }))  // what its job last wrote: doctor isn't run
        }
        for (args, put) in reads {
            group.enter()
            cli.run(args, timeout: 30) { put($0); group.leave() }
        }
        group.notify(queue: .main) {
            var next = self.snapshot
            next.services = services.flatMap { $0.ok ? decode(ServicesStatus.self, $0.out) : nil }
            next.workers = workers.flatMap { $0.ok ? decode([WorkerRow].self, $0.out) : nil }
            next.modules = modules.flatMap { $0.ok ? decode([Module].self, $0.out) : nil }
            if readDoctor { next.doctor = doctor.flatMap { $0.ok ? decode(DoctorLast.self, $0.out) : nil } }
            self.snapshot = next
            self.refreshing -= 1
            self.polls += 1
            self.redraw()
            if self.again {
                self.again = false
                self.refresh()
            }
        }
    }

    func redraw() {
        if open {
            stale = true
            updateIcon()
            return
        }
        stale = false
        menu.removeAllItems()
        for node in menuNodes(snapshot) { menu.addItem(makeItem(node)) }
        updateIcon()
    }

    /// A fresh read of doctor's last pass with the next poll: after Run, Quit or Start, what it says may have changed.
    private func rereadDoctor() {
        doctorReadAt = .distantPast
    }

    private func makeItem(_ node: Node) -> NSMenuItem {
        if case let .warning(title, children) = node {
            let item = makeItem(.item(title, nil, children))
            item.tag = Controller.warningTag
            item.image = NSImage(systemSymbolName: "exclamationmark.triangle.fill", accessibilityDescription: "warning")
            item.attributedTitle = NSAttributedString(string: title, attributes: [.foregroundColor: NSColor.systemOrange])
            return item
        }
        if case let .part(name, mark, command) = node {
            let item = makeItem(.item(name, command, nil))
            // the mark as coloured text: a menu item's image isn't drawn in today's menus, and text is, dimmed with a disabled row
            let colour: NSColor = mark == .ok ? .systemGreen : mark == .bad ? .systemRed : .systemOrange
            let font = NSFont.menuFont(ofSize: 0)
            let title = NSMutableAttributedString(string: mark.rawValue, attributes: [.foregroundColor: colour, .font: font])
            title.append(NSAttributedString(string: " ", attributes: [.font: font, .kern: 4]))  // its title reads "✓ dash"
            title.append(NSAttributedString(string: name, attributes: [
                .foregroundColor: command == nil ? NSColor.secondaryLabelColor : NSColor.labelColor, .font: font]))
            item.attributedTitle = title
            return item
        }
        guard case let .item(title, command, children) = node else { return .separator() }
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        if let children = children {
            let sub = NSMenu(title: title)
            sub.autoenablesItems = false
            for child in children { sub.addItem(makeItem(child)) }
            item.submenu = sub
            item.isEnabled = true
        } else if let command = command {
            item.target = self
            item.action = #selector(clicked(_:))
            item.representedObject = Box(command)
            item.isEnabled = !busy.contains(title)
        } else {
            item.isEnabled = false
        }
        return item
    }

    // MARK: acting

    @objc func clicked(_ sender: NSMenuItem) {
        guard let command = (sender.representedObject as? Box)?.command else { return }
        let title = sender.title
        busy.insert(title)
        switch command {
        case .run(let args):
            cli.run(args) { result in
                self.busy.remove(title)
                if !result.ok { self.show(error: "\(title): \(result.problem)") }
                self.refresh()
            }
        case .module(let name, let id):
            snapshot.moduleErrors[name] = nil
            let shown = snapshot.modules?.first { $0.name == name }?.title ?? name
            cli.run(["modules", "action", name, id]) { result in
                self.busy.remove(title)
                if result.ok, let module = decode(Module.self, result.out) {
                    self.snapshot.replace(module)  // the contract prints the new object
                } else {
                    self.snapshot.moduleErrors[name] = result.ok ? "unreadable answer from modules action" : result.problem
                    self.show(error: "\(shown): \(self.snapshot.moduleErrors[name] ?? "")")
                }
                self.redraw()
            }
        case .quit:
            // services stop takes the watchdog down first, then athena's other jobs; Rex and its sessions run on. The app
            // stays, showing athena stopped, so Start athena is one click away.
            cli.run(["services", "stop"], timeout: 180) { result in
                self.busy.remove(title)
                if !result.ok { self.show(error: "Quit athena: \(result.problem)") }
                self.rereadDoctor()
                self.refresh()
            }
        case .start:
            cli.run(["services", "start"], timeout: 180) { result in
                self.busy.remove(title)
                if !result.ok { self.show(error: "Start athena: \(result.problem)") }
                self.rereadDoctor()
                self.refresh()
            }
        case .doctorFix:
            cli.run(["doctor", "--fix", "--quiet"], timeout: 300) { result in
                self.busy.remove(title)
                if result.code != 0 && result.code != 1 { self.show(error: "Run doctor --fix: \(result.problem)") }
                self.rereadDoctor()
                self.refresh()
            }
        }
        redraw()
    }

    private func show(error text: String) {
        if headless {
            print("error: \(text)")
            return
        }
        let alert = NSAlert()
        alert.messageText = "athena-rex"
        alert.informativeText = text
        alert.alertStyle = .warning
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    // MARK: the status item

    func showStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.menu = menu
        self.item = item
        updateIcon()
        refresh()
        let poll = Timer(timeInterval: 5, repeats: true) { [weak self] _ in self?.refresh() }
        RunLoop.main.add(poll, forMode: .common)
    }

    func menuWillOpen(_ menu: NSMenu) {
        open = true
    }

    func menuDidClose(_ menu: NSMenu) {
        open = false
        // a turn later: AppKit sends the clicked item's action after this, and the item must still be there to get it
        DispatchQueue.main.async {
            if self.stale { self.redraw() }
            self.refresh()
        }
    }

    private func updateIcon() {
        guard let button = item?.button else { return }
        let icon = snapshot.icon
        button.toolTip = "athena: \(snapshot.summary)"
        if icon != .working {
            animation?.invalidate()
            animation = nil
            button.image = knot.image(icon)
            return
        }
        guard animation == nil else { return }
        let begun = Date()
        let tick = Timer(timeInterval: 1.0 / 25, repeats: true) { [weak self] _ in
            let t = Date().timeIntervalSince(begun).truncatingRemainder(dividingBy: Knot.cycle) / Knot.cycle
            self?.item?.button?.image = self?.knot.image(.working, phase: t)
        }
        RunLoop.main.add(tick, forMode: .common)
        animation = tick
        tick.fire()
    }

    // MARK: headless

    /// Runs the main loop until every command it started has answered.
    func settle(limit: TimeInterval = 200) {
        let end = Date().addingTimeInterval(limit)
        repeat {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
        } while (cli.pending > 0 || refreshing > 0) && Date() < end
    }

    /// The menu as text: a disabled item in brackets, a submenu's items indented under its title and its `>`, ⚠ before a
    /// warning.
    func dump() -> String {
        func walk(_ menu: NSMenu, _ indent: String) -> [String] {
            menu.items.flatMap { item -> [String] in
                if item.isSeparatorItem { return [indent + "---"] }
                let mark = item.tag == Controller.warningTag ? "⚠ " : ""
                if let sub = item.submenu { return [indent + mark + item.title + " >"] + walk(sub, indent + "  ") }
                return [indent + (item.isEnabled ? item.title : "(\(item.title))")]
            }
        }
        return (walk(menu, "") + ["icon: \(snapshot.icon.rawValue)"]).joined(separator: "\n")
    }

    /// Clicks the item at a path of titles, "Clamshell: on > Turn off", as a person would.
    func click(_ path: String) -> Bool {
        var menu = self.menu
        let titles = path.components(separatedBy: " > ")
        for (n, title) in titles.enumerated() {
            guard let at = menu.items.firstIndex(where: { $0.title == title }) else { return false }
            if n == titles.count - 1 {
                let item = menu.items[at]
                // the item's own target and action; NSMenu's performActionForItem needs NSApp, which headless has none of
                guard item.isEnabled, let target = item.target as? NSObject, let action = item.action else { return false }
                target.perform(action, with: item)
                return true
            }
            guard let sub = menu.items[at].submenu else { return false }
            menu = sub
        }
        return false
    }
}

extension Controller {
    static let warningTag = 7
}

final class Box: NSObject {
    let command: Command
    init(_ command: Command) { self.command = command }
}
