// Loads the app's whiteboard page (VEL's bundled stage) in WebKit and plays two steps, as the app will.
import AppKit
import WebKit

final class Probe: NSObject, WKNavigationDelegate {
    let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 500))
    func run() {
        web.navigationDelegate = self
        let dir = URL(fileURLWithPath: CommandLine.arguments[1])
        web.loadFileURL(dir.appendingPathComponent("whiteboard.html"), allowingReadAccessTo: dir)
    }
    func webView(_ w: WKWebView, didFinish _: WKNavigation!) {
        let lesson = """
        [{"say":"This tab holds the fan to the base.","svg":"<svg viewBox='0 0 800 500'><rect x='100' y='300' width='600' height='40' fill='none' stroke='currentColor' stroke-width='4'/><text x='110' y='290' font-size='28'>base</text></svg>"},
         {"say":"Here is where it cracked.","svg":"<svg viewBox='0 0 800 500'><path class='draw' d='M400 300 L420 260 L410 220' fill='none' stroke='#e5484d' stroke-width='5'/><circle cx='410' cy='260' r='50' fill='none' stroke='#e5484d' stroke-width='3'/></svg>"}]
        """
        Task { @MainActor in
            do {
                let n = try await w.evaluateJavaScript("play(\(lesson))")
                let d1 = try await w.evaluateJavaScript("step(0)")
                try await Task.sleep(for: .seconds(2))
                let d2 = try await w.evaluateJavaScript("step(1)")
                try await Task.sleep(for: .seconds(2))
                let ink = try await w.evaluateJavaScript("inkCount()")
                print("steps=\(n) durations=\(d1),\(d2) inkElements=\(ink)")
                exit((ink as? Int ?? 0) > 0 && (d1 as? Double ?? -1) >= 0 && (d2 as? Double ?? -1) >= 0 ? 0 : 1)
            } catch { print("JS error: \(error)"); exit(1) }
        }
    }
}
let app = NSApplication.shared
let probe = Probe()
probe.run()
DispatchQueue.main.asyncAfter(deadline: .now() + 30) { print("timeout"); exit(2) }
app.run()
