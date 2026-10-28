import XCTest

/// Opens every screen in a fixed demo state (`-screens <name>`, ios/RepairOS/App/ScreensHarness.swift) and saves a
/// screenshot of it. The CI `screens` job runs this on two simulators, in light and dark, and once at the largest
/// accessibility text size, and uploads the pictures as the `screens` artifact (ios/README.md, "Seeing the app from
/// Windows").
///
/// The name is `<screen>-<device>-<appearance>-<textsize>.png`; the job passes the last three through
/// `TEST_RUNNER_SCREENS_DEVICE`, `_APPEARANCE` and `_TEXT` (the simulator is set up with `simctl ui` before the run).
/// With `SCREENS_OUT` set the PNGs are also written to that folder, and with `SCREENS_AUDIT=1` the system's
/// accessibility audit runs on each screen and its findings are saved as `<screen>-...-audit.txt`. The audit never
/// fails a test: what it finds is for people to read.
@MainActor
final class ScreenshotTests: XCTestCase {
    func testConsent() { shoot("consent") }
    func testHomeEmpty() { shoot("home-empty") }
    func testHomeScans() { shoot("home-scans") }
    func testSettings() { shoot("settings") }
    func testPairing() { shoot("pairing") }
    func testReport() { shoot("report", settle: 4) }
    func testMeter() { shoot("meter") }
    func testCapture() { shoot("capture") }
    func testCaptureLocked() { shoot("capture-locked") }
    func testCameraPermission() { shoot("camera-permission") }
    func testProgressError() { shoot("progress-error") }
    func testProductCode() { shoot("product-code") }
    func testProductPairing() { shoot("product-pairing") }
    func testFailures() { shoot("failures") }

    // MARK: -

    private func shoot(_ screen: String, settle: TimeInterval = 1.5) {
        let env = ProcessInfo.processInfo.environment
        let device = env["SCREENS_DEVICE"] ?? "device"
        let appearance = env["SCREENS_APPEARANCE"] ?? "light"
        let text = env["SCREENS_TEXT"] ?? "default"
        let stem = "\(screen)-\(device)-\(appearance)-\(text)"

        let app = XCUIApplication()
        app.launchArguments = ["-screens", screen]
        app.launch()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 30), "\(screen): the app did not stay in the foreground")
        Thread.sleep(forTimeInterval: settle)

        let png = XCUIScreen.main.screenshot().pngRepresentation
        let picture = XCTAttachment(data: png, uniformTypeIdentifier: "public.png")
        picture.name = "\(stem).png"
        picture.lifetime = .keepAlways
        add(picture)
        if let out = env["SCREENS_OUT"], !out.isEmpty {
            let dir = URL(fileURLWithPath: out, isDirectory: true)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try? png.write(to: dir.appending(path: "\(stem).png"))
        }

        if env["SCREENS_AUDIT"] == "1" { audit(app, stem: stem, out: env["SCREENS_OUT"]) }
        app.terminate()
    }

    /// Xcode's accessibility audit (contrast, tap target size, clipped or unlabelled text) as a plain text list.
    private func audit(_ app: XCUIApplication, stem: String, out: String?) {
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
