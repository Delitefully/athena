import AppKit

// AthenaRexMenu            the menu bar app, as its login job runs it (`athena-rex menubar run`)
// --dump-menu              the menu as text, read from athena-rex, and exit
// --click "<a> > <b>"      click that item, wait for what it runs, then print the menu
// --refresh-burst <n>     ask for n refreshes at once, as a stalled timer would, and print how many polls ran
// --render <state> <t> <out.png> [dark]   Knot as the menu bar would tint it, for a preview
// --dash <t>               working's dash for each strand at phase t

let args = Array(CommandLine.arguments.dropFirst())

func fail(_ text: String) -> Never {
    FileHandle.standardError.write(Data((text + "\n").utf8))
    exit(2)
}

switch args.first {
case "--dump-menu", "--click":
    let controller = Controller(headless: true)
    controller.refresh()
    controller.settle()
    if args.first == "--click" {
        guard args.count == 2 else { fail("usage: --click \"<title> > <title>\"") }
        guard controller.click(args[1]) else { fail("no item to click at \(args[1])\n\(controller.dump())") }
        controller.settle()
    }
    print(controller.dump())
case "--refresh-burst":
    guard args.count == 2, let n = Int(args[1]) else { fail("usage: --refresh-burst <n>") }
    let controller = Controller(headless: true)
    for _ in 0..<n { controller.refresh() }
    controller.settle()
    print("polls: \(controller.polls)")
case "--render":
    guard args.count >= 4, let icon = Icon(rawValue: args[1]), let t = Double(args[2]) else {
        fail("usage: --render idle|working|needs-you|stopped <phase> <out.png> [dark]")
    }
    guard let png = Knot().png(icon, phase: t, dark: args.count > 4 && args[4] == "dark") else { fail("no glyph for \(args[1])") }
    do { try png.write(to: URL(fileURLWithPath: args[3])) } catch { fail("\(args[3]): \(error.localizedDescription)") }
case "--dash":
    guard args.count == 2, let t = Double(args[1]) else { fail("usage: --dash <phase>") }
    for (i, strand) in Knot().strands.enumerated() {
        let dash = Knot.dash(strand: i, length: strand.length, phase: t)
        print("strand \(i): length \(String(format: "%.2f", strand.length)) dash " +
              (dash?.map { String(format: "%.2f", $0) }.joined(separator: " ") ?? "solid"))
    }
case nil:
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let controller = Controller(headless: false)
    controller.showStatusItem()
    app.run()
default:
    fail("unknown argument \(args[0]); see the top of menubar/Sources/main.swift")
}
