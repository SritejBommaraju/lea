import SwiftUI

/// The side ring on the capture screen: twelve directions round the object, filled once a sharp photo was taken from
/// there, with the one to go to next marked and the phone's own direction shown by a pointer. Plain on purpose: the
/// number in the middle is "sides seen of sides wanted" (the same count the sentence under it uses).
/// Directions are those of `CoverageDome` (sector 0 at the bottom, running toward the right).
struct SideRing: View {
    /// Sectors (0..<12) with a photo from the side.
    let seen: Set<Int>
    var here: Int?
    var next: Int?
    /// The scan has enough.
    var done = false
    /// Sides seen and wanted.
    let count: Int
    let target: Int
    var size: CGFloat = 76

    static let sectors = CoverageDome.sectors

    var body: some View {
        ZStack {
            Circle().fill(Palette.smoke)
            ForEach(0..<Self.sectors, id: \.self) { s in
                SectorBand(sector: s, sectors: Self.sectors)
                    .fill(fill(s))
                    .overlay(SectorBand(sector: s, sectors: Self.sectors)
                        .stroke(seen.contains(s) ? Color.clear : Palette.chalk.opacity(0.55), lineWidth: Line.construction))
            }
            if let next, !done {
                SectorBand(sector: next, sectors: Self.sectors)
                    .stroke(Palette.next, lineWidth: Line.locked)
            }
            if let here {
                SectorPointer(sector: here, sectors: Self.sectors).fill(Palette.chalk)
            }
            Text("\(min(count, target))/\(target)")
                .font(.system(.footnote, design: .rounded).weight(.bold))
                .monospacedDigit()
                .foregroundStyle(Palette.chalk)
        }
        .frame(width: size, height: size)
        .drawingGroup()
        .motion(Motion.fade, value: seen)
        .motion(Motion.standard, value: here)
        .accessibilityElement()
        .accessibilityLabel("Sides photographed")
        .accessibilityValue("\(min(count, target)) of \(target)")
    }

    private func fill(_ s: Int) -> Color {
        guard seen.contains(s) else { return .clear }
        return done ? Palette.cameraGood : Palette.chalk.opacity(0.85)
    }
}

/// One twelfth of the ring.
private struct SectorBand: Shape {
    let sector: Int
    let sectors: Int

    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        let outer = rect.width / 2 - rect.width * 0.06, inner = rect.width / 2 - rect.width * 0.24
        let a0 = Double(sector) * 2 * .pi / Double(sectors) + 0.04
        let a1 = Double(sector + 1) * 2 * .pi / Double(sectors) - 0.04
        func point(_ r: CGFloat, _ a: Double) -> CGPoint { CGPoint(x: c.x + r * CGFloat(sin(a)), y: c.y + r * CGFloat(cos(a))) }
        var p = Path()
        let steps = 6
        p.move(to: point(outer, a0))
        for i in 1...steps { p.addLine(to: point(outer, a0 + (a1 - a0) * Double(i) / Double(steps))) }
        for i in 0...steps { p.addLine(to: point(inner, a1 - (a1 - a0) * Double(i) / Double(steps))) }
        p.closeSubpath()
        return p
    }
}

/// A small pointer on the ring's inside edge, toward the sector the phone is in.
private struct SectorPointer: Shape {
    let sector: Int
    let sectors: Int

    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        let a = (Double(sector) + 0.5) * 2 * .pi / Double(sectors)
        let r = rect.width / 2 - rect.width * 0.27
        func point(_ radius: CGFloat, _ angle: Double) -> CGPoint {
            CGPoint(x: c.x + radius * CGFloat(sin(angle)), y: c.y + radius * CGFloat(cos(angle)))
        }
        var p = Path()
        p.move(to: point(r, a))
        p.addLine(to: point(r - rect.width * 0.1, a - 0.22))
        p.addLine(to: point(r - rect.width * 0.1, a + 0.22))
        p.closeSubpath()
        return p
    }
}

/// What the customer sees when they tap Done: what was captured, and one sentence for each thing that is wrong with
/// it, each with the fix. A good scan gets one button. A scan with problems can go back to scanning, or be sent anyway
/// (the report then says what the photos could not show).
struct CaptureReviewView: View {
    let report: QualityReport
    /// Go on to the report.
    let onContinue: () -> Void
    /// Back to the scanner for the same scan; nil where that is not possible.
    let onKeepScanning: (() -> Void)?

    private var good: Bool { report.problems.isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.l) {
            Spacer()
            Image(systemName: good ? "checkmark.circle" : "exclamationmark.triangle")
                .font(.system(size: 34, weight: .regular))
                .foregroundStyle(good ? Palette.good : Palette.caution)
                .accessibilityHidden(true)
            Text(good ? "Scan saved" : "Check this scan").display(.screen).foregroundStyle(Palette.ink)
                .accessibilityAddTraits(.isHeader)
            Text(Self.summary(report)).font(.body).foregroundStyle(Palette.secondary)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: Spacing.m) {
                ForEach(Array((report.problems + report.notes).prefix(5)), id: \.id) { finding in
                    HStack(alignment: .top, spacing: Spacing.m) {
                        Image(systemName: finding.severity == .note ? "info.circle" : "exclamationmark.circle")
                            .foregroundStyle(finding.severity == .blocker ? Palette.danger
                                             : (finding.severity == .warning ? Palette.caution : Palette.secondary))
                            .accessibilityHidden(true)
                        Text(finding.message).font(.body).foregroundStyle(Palette.ink)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            Spacer()
            if good {
                Button(action: onContinue) { Label("See the report", systemImage: "arrow.right") }
                    .buttonStyle(.primary)
            } else {
                if let onKeepScanning {
                    Button(action: onKeepScanning) { Label("Keep scanning", systemImage: Symbol.scan) }
                        .buttonStyle(.primary)
                }
                Button(action: onContinue) { Text("Upload anyway") }
                    .buttonStyle(.primary(onKeepScanning == nil ? .accent : .quiet))
            }
        }
        .padding(.horizontal, Spacing.gutter)
        .padding(.bottom, Spacing.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.canvas.ignoresSafeArea())
    }

    /// "12 photos, 3 close-ups, 8 of 8 sides." (what the phone has, in the customer's words)
    static func summary(_ r: QualityReport) -> String {
        var parts = ["\(r.stills.count) photo\(r.stills.count == 1 ? "" : "s")"]
        if r.stills.closeUps > 0 { parts.append("\(r.stills.closeUps) close-up\(r.stills.closeUps == 1 ? "" : "s")") }
        parts.append("\(min(r.coverage.sideSectors, r.coverage.sideTarget)) of \(r.coverage.sideTarget) sides")
        return parts.joined(separator: ", ") + "."
    }
}
