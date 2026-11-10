import AppKit

/// Knot, the status item's glyph. Idle, needs-you and stopped are the bundled SVGs, loaded as template images so the menu bar
/// tints them for light, dark and highlight. Working is drawn here, from the strands in knot-working.svg: one gap opens in
/// each strand, runs along it and closes again, the strands a third of a loop apart, over 2.4 s. No frame files.
final class Knot {
    static let size = NSSize(width: 22, height: 22)
    static let cycle: TimeInterval = 2.4
    // the mark's 100-unit box to the glyph, as in the SVGs' transform: about 15.5 pt of ink in the 22 pt bar, a system icon's padding
    static let scale: CGFloat = 0.18
    static let lineWidth: CGFloat = 8.14 * scale
    static let widestGap: CGFloat = 4.6  // points; the round caps cover 1.47 of it
    static let run: (CGFloat, CGFloat) = (0.24, 0.76)  // the gap's centre travels this share of its strand, never to an end

    private var images: [Icon: NSImage] = [:]
    private(set) var strands: [(path: CGPath, length: CGFloat)] = []

    init(bundle: Bundle = .main) {
        for icon in [Icon.idle, .needsYou, .stopped, .working] {
            guard let url = bundle.url(forResource: "knot-\(icon.rawValue)", withExtension: "svg"),
                  let image = NSImage(contentsOf: url) else { continue }
            image.size = Knot.size
            image.isTemplate = true
            image.accessibilityDescription = "athena: \(icon == .needsYou ? "needs you" : icon.rawValue)"
            images[icon] = image
        }
        if let url = bundle.url(forResource: "knot-working", withExtension: "svg"),
           let text = try? String(contentsOf: url, encoding: .utf8) {
            strands = Knot.paths(in: text).map { ($0, Knot.length(of: $0)) }
        }
    }

    /// The glyph for a state; working at phase t of its loop (0 to 1).
    func image(_ icon: Icon, phase t: Double = 0) -> NSImage? {
        guard icon == .working, strands.count == 3 else { return images[icon] }
        let image = NSImage(size: Knot.size, flipped: true) { _ in
            guard let cg = NSGraphicsContext.current?.cgContext else { return false }
            cg.setStrokeColor(NSColor.black.cgColor)
            cg.setLineWidth(Knot.lineWidth)
            cg.setLineCap(.round)
            for (i, strand) in self.strands.enumerated() {
                cg.saveGState()
                if let dash = Knot.dash(strand: i, length: strand.length, phase: t) {
                    cg.setLineDash(phase: 0, lengths: dash)
                }
                cg.addPath(strand.path)
                cg.strokePath()
                cg.restoreGState()
            }
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "athena: working"
        return image
    }

    /// Strand i's dash at phase t: a dash up to the gap, the gap, and the rest. Nil when the gap is closed, so it is solid.
    /// The gap's width follows sin² and its centre a smooth ease, so both start and stop at rest; the three widths always
    /// add up to the same, so the glyph keeps its weight and never blinks.
    static func dash(strand i: Int, length: CGFloat, phase t: Double) -> [CGFloat]? {
        var u = (t - Double(i) / 3).truncatingRemainder(dividingBy: 1)
        if u < 0 { u += 1 }
        let width = widestGap * CGFloat(pow(sin(Double.pi * u), 2))
        let eased = CGFloat(u - sin(2 * Double.pi * u) / (2 * Double.pi))
        let centre = length * (run.0 + (run.1 - run.0) * eased)
        if width < 0.01 { return nil }
        return [centre - width / 2, width, 2 * length]
    }

    /// The `d` of each path in an SVG of Knot's, absolute M and C only, placed by the SVGs' own transform:
    /// translate(11 11.1) scale(0.18) translate(-50 -50), in a top-left space.
    static func paths(in svg: String) -> [CGPath] {
        guard let attr = try? NSRegularExpression(pattern: "\\sd=\"([^\"]+)\""),
              let token = try? NSRegularExpression(pattern: "[MC]|-?[0-9]*\\.?[0-9]+") else { return [] }
        let place = { (x: CGFloat, y: CGFloat) in CGPoint(x: 11 + scale * (x - 50), y: 11.1 + scale * (y - 50)) }
        var out: [CGPath] = []
        let whole = NSRange(svg.startIndex..., in: svg)
        for match in attr.matches(in: svg, range: whole) {
            let d = (svg as NSString).substring(with: match.range(at: 1))
            let words = token.matches(in: d, range: NSRange(d.startIndex..., in: d)).map { (d as NSString).substring(with: $0.range) }
            let path = CGMutablePath()
            var command = "", numbers: [CGFloat] = []
            func flush() {
                if command == "M", numbers.count >= 2 {
                    path.move(to: place(numbers[0], numbers[1]))
                } else if command == "C" {
                    var k = 0
                    while k + 6 <= numbers.count {
                        path.addCurve(to: place(numbers[k + 4], numbers[k + 5]), control1: place(numbers[k], numbers[k + 1]),
                                      control2: place(numbers[k + 2], numbers[k + 3]))
                        k += 6
                    }
                }
                numbers = []
            }
            for word in words {
                if word == "M" || word == "C" {
                    flush()
                    command = word
                } else if let value = Double(word) {
                    numbers.append(CGFloat(value))
                }
            }
            flush()
            out.append(path)
        }
        return out
    }

    static func length(of path: CGPath, steps: Int = 64) -> CGFloat {
        var total: CGFloat = 0
        var current = CGPoint.zero
        path.applyWithBlock { element in
            let e = element.pointee
            switch e.type {
            case .moveToPoint:
                current = e.points[0]
            case .addCurveToPoint:
                let (a, b, c) = (e.points[0], e.points[1], e.points[2])
                var previous = current
                for s in 1...steps {
                    let t = CGFloat(s) / CGFloat(steps), m = 1 - t
                    let q = CGPoint(x: m * m * m * current.x + 3 * m * m * t * a.x + 3 * m * t * t * b.x + t * t * t * c.x,
                                    y: m * m * m * current.y + 3 * m * m * t * a.y + 3 * m * t * t * b.y + t * t * t * c.y)
                    total += hypot(q.x - previous.x, q.y - previous.y)
                    previous = q
                }
                current = c
            default:
                break
            }
        }
        return total
    }

    /// A preview: the glyph tinted as the menu bar would, on a light or dark bar, `zoom` times its size.
    func png(_ icon: Icon, phase: Double, dark: Bool, zoom: CGFloat = 8) -> Data? {
        guard let glyph = image(icon, phase: phase) else { return nil }
        let pixels = Int(Knot.size.width * zoom)
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                                         samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                         bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
        rep.size = Knot.size
        let rect = NSRect(origin: .zero, size: Knot.size)
        let tint = dark ? NSColor.white : NSColor.black
        let tinted = NSImage(size: Knot.size, flipped: false) { r in
            glyph.draw(in: r)
            tint.set()
            r.fill(using: .sourceAtop)
            return true
        }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        (dark ? NSColor(white: 0.16, alpha: 1) : NSColor(white: 0.93, alpha: 1)).setFill()
        rect.fill()
        tinted.draw(in: rect)
        NSGraphicsContext.restoreGraphicsState()
        return rep.representation(using: .png, properties: [:])
    }
}
