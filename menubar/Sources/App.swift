import AppKit

/// The menu and what its items do. Everything it knows comes from athena and everything it does goes through it:
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

    /// One poll at a time: asked while one runs, it runs once more when that one answers. A slow athena never gets a
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
        if case let .check(row, ok, command) = node {
            let item = makeItem(.item(row, command, nil))
            let font = NSFont.menuFont(ofSize: 0)  // its title reads "✗ disk: 3 GB free", its mark red as a part's is
            let title = NSMutableAttributedString(string: String(row.prefix(1)), attributes: [
                .foregroundColor: ok ? NSColor.systemGreen : NSColor.systemRed, .font: font])
            title.append(NSAttributedString(string: String(row.dropFirst()), attributes: [.foregroundColor: NSColor.labelColor, .font: font]))
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
        case .detail(let name, let detail):
            busy.remove(title)
            show(detail: detail, of: name)
            return
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
        alert.messageText = "athena"
        alert.informativeText = text
        alert.alertStyle = .warning
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    static let detailWidth: CGFloat = 360  // a readable measure, and well inside 480 px

    /// A Doctor check's whole detail, wrapped, selectable, and with Copy, which puts "name: detail" on the pasteboard.
    /// Headless, it prints what Copy would copy.
    private func show(detail: String, of name: String) {
        let whole = "\(name): \(detail)"
        if headless {
            print("detail: \(whole)")
            return
        }
        let text = NSTextField(wrappingLabelWithString: detail.isEmpty ? "(doctor said nothing more)" : detail)
        text.isSelectable = true
        text.preferredMaxLayoutWidth = Controller.detailWidth
        text.frame = NSRect(origin: .zero, size: NSSize(width: Controller.detailWidth, height: text.fittingSize.height))
        let alert = NSAlert()
        alert.messageText = name
        alert.accessoryView = text
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Copy")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertSecondButtonReturn {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(whole, forType: .string)
        }
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

    /// For a screenshot: once the first poll has answered, the submenu at `path` pops open under the status item (a submenu
    /// can't be opened from code while its menu is), or the Doctor row there shows its detail, and nothing else is ever
    /// clicked, so a preview can't quit or restart anything; a second later this app's windows are
    /// printed as "window <id> <x> <y> <w> <h>", top-left points, for screencapture.
    func preview(_ path: String) {
        let wait = Timer(timeInterval: 0.1, repeats: true) { [weak self] timer in
            guard let self = self, self.polls > 0 else { return }
            timer.invalidate()
            let windows = Timer(timeInterval: 1.5, repeats: false) { _ in Controller.printWindows() }
            RunLoop.main.add(windows, forMode: .common)
            NSApp.activate(ignoringOtherApps: true)  // a menu popped by an app in the background closes at once
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self.previewOpen(path) }
        }
        RunLoop.main.add(wait, forMode: .common)
    }

    private func previewOpen(_ path: String) {
        var menu = self.menu
        var found: NSMenuItem?
        for title in path.components(separatedBy: " > ") {
            guard let item = menu.items.first(where: { $0.title == title }) else { fail("no item at \(path)\n\(dump())") }
            found = item
            if let sub = item.submenu { menu = sub }
        }
        guard let item = found else { return }
        if let sub = item.submenu {
            item.submenu = nil  // a menu with a supermenu won't pop up on its own
            // under the status item, a little lower than its bottom edge: at it, AppKit takes the menu's top as off screen
            // and scrolls its first row away
            let frame = self.item?.button?.window?.frame ?? .zero
            sub.popUp(positioning: nil, at: NSPoint(x: frame.minX, y: frame.minY - 16), in: nil)
        } else if case .detail(let name, let detail)? = (item.representedObject as? Box)?.command {
            show(detail: detail, of: name)
        } else {
            fail("--preview opens a submenu or a Doctor row's detail, and \(path) is neither")
        }
    }

    private static func printWindows() {
        let mine = ProcessInfo.processInfo.processIdentifier
        let all = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
        for window in all where (window[kCGWindowOwnerPID as String] as? Int32) == mine {
            guard let id = window[kCGWindowNumber as String] as? Int,
                  let bounds = window[kCGWindowBounds as String] as? [String: CGFloat] else { continue }
            print("window \(id) \(Int(bounds["X"] ?? 0)) \(Int(bounds["Y"] ?? 0)) \(Int(bounds["Width"] ?? 0)) \(Int(bounds["Height"] ?? 0))")
        }
        fflush(stdout)
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
