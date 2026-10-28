import XCTest

/// Opens every screen in a fixed demo state and saves a screenshot of it. The CI `screens` job runs this on two
/// simulators, in light and dark, and once at the largest accessibility text size, and uploads the pictures as the
/// `screens` artifact (ios/README.md, "Seeing the app from Windows").
///
/// The app is launched once with `-screens walk` (ios/RepairOS/App/ScreensGallery.swift): it shows each screen in turn
/// and moves on by itself; the label of an invisible marker (`screens.next`) is the name of the screen on show.
///
/// The picture is named `<screen>-<device>-<appearance>-<textsize>.png`; the job passes the last three through
/// `TEST_RUNNER_SCREENS_DEVICE`, `_APPEARANCE` and `_TEXT` (the simulator is set up with `simctl ui` before the run).
/// With `SCREENS_OUT` set the PNGs are also written to that folder, and with `SCREENS_AUDIT=1` the system's
/// accessibility audit runs on each screen and its findings are saved as `<screen>-...-audit.txt`. The audit never
/// fails a test: what it finds is for people to read.
@MainActor
final class ScreenshotTests: XCTestCase {
    /// Screens that need longer to draw (the report builds a 3D stage).
    private let settle: [String: TimeInterval] = ["report": 4]

    func testWalkEveryScreen() {
        let env = ProcessInfo.processInfo.environment
        let device = env["SCREENS_DEVICE"] ?? "device"
        let appearance = env["SCREENS_APPEARANCE"] ?? "light"
        let text = env["SCREENS_TEXT"] ?? "default"

        let audit = env["SCREENS_AUDIT"] == "1"
        let app = XCUIApplication()
        // Each screen stays up for the dwell, then the app moves on by itself: long enough to settle, shoot, and (with
        // the audit, which takes seconds) to audit.
        app.launchArguments = ["-screens", "walk", "-screens-dwell", audit ? "40" : "7", "-screens-start-delay", "15"]
        app.launch()
        let current = app.buttons["screens.next"]
        XCTAssertTrue(current.waitForExistence(timeout: 30), "the screens marker never appeared")

        // The app asks for the microphone and speech recognition as soon as it opens (a finding in itself). Allow both, so the
        // alerts do not sit over every picture.
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for _ in 0..<2 {
            let allow = springboard.alerts.buttons["Allow"]
            if allow.waitForExistence(timeout: 4) { allow.tap() }
        }

        var taken: [String] = []
        var last = "start"
        var lastChange = Date()
        // Longer than the longest a screen stays up (the report, plus the audit's dwell), so a slow screen never ends the walk.
        let giveUp: TimeInterval = audit ? 120 : 45
        while Date().timeIntervalSince(lastChange) < giveUp {
            let screen = current.label
            if screen == "done" { break }
            if screen == last || screen == "start" { Thread.sleep(forTimeInterval: 0.25); continue }
            Thread.sleep(forTimeInterval: settle[screen] ?? 1.5)
            // The label is read through a slow accessibility query; if the walk has moved on while it settled, this
            // screen is gone, so do not file the next screen's picture under its name.
            guard current.label == screen else { continue }
            last = screen
            lastChange = Date()
            XCTAssertTrue(app.state == .runningForeground, "\(screen): the app is no longer running")
            shoot(app, stem: "\(screen)-\(device)-\(appearance)-\(text)", out: env["SCREENS_OUT"], audit: audit)
            taken.append(screen)
        }
        app.terminate()

        // A screen the walk went past while a slow accessibility query was answering is taken again on its own: a fresh
        // launch straight to that screen.
        for screen in Self.allScreens where !taken.contains(screen) {
            let single = XCUIApplication()
            single.launchArguments = ["-screens", screen]
            single.launch()
            Thread.sleep(forTimeInterval: settle[screen] ?? 2.5)
            shoot(single, stem: "\(screen)-\(device)-\(appearance)-\(text)", out: env["SCREENS_OUT"], audit: audit)
            taken.append(screen)
            single.terminate()
        }
        XCTAssertEqual(Set(taken), Set(Self.allScreens), "screens taken: \(taken)")
    }

    /// The screens `-screens walk` shows (ScreensGallery.walkOrder).
    private static let allScreens = ["consent", "home-empty", "home-scans", "settings", "pairing", "product-code", "product-pairing",
                                     "report", "meter", "capture", "capture-locked", "camera-permission", "progress-error", "failures"]

    // MARK: -

    private func shoot(_ app: XCUIApplication, stem: String, out: String?, audit: Bool) {
        let png = XCUIScreen.main.screenshot().pngRepresentation
        let picture = XCTAttachment(data: png, uniformTypeIdentifier: "public.png")
        picture.name = "\(stem).png"
        picture.lifetime = .keepAlways
        add(picture)
        if let out, !out.isEmpty {
            let dir = URL(fileURLWithPath: out, isDirectory: true)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try? png.write(to: dir.appending(path: "\(stem).png"))
        }
        if audit { runAudit(app, stem: stem, out: out) }
    }

    /// Xcode's accessibility audit (contrast, tap target size, clipped or unlabelled text) as a plain text list.
    private func runAudit(_ app: XCUIApplication, stem: String, out: String?) {
        guard #available(iOS 17.0, *) else { return }
        var lines: [String] = []
        do {
            try app.performAccessibilityAudit { issue in
                lines.append("type \(issue.auditType.rawValue): \(issue.compactDescription) | \(issue.detailedDescription) | \(issue.element?.debugDescription ?? "no element")")
                return true   // handled: never a test failure
            }
        } catch {
            lines.append("audit could not run: \(error)")
        }
        let text = lines.isEmpty ? "no issues" : lines.joined(separator: "\n")
        let note = XCTAttachment(string: text)
        note.name = "\(stem)-audit.txt"
        note.lifetime = .keepAlways
        add(note)
        if let out, !out.isEmpty {
            try? text.write(to: URL(fileURLWithPath: out, isDirectory: true).appending(path: "\(stem)-audit.txt"), atomically: true, encoding: .utf8)
        }
    }
}
