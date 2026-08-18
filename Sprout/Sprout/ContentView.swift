import SwiftUI
import WebKit

// IMPORTANT: replace this with your real deployed backend URL before
// building — it must be a real https:// address (not 127.0.0.1, not a
// Tailscale 100.x address). Apple's reviewers cannot reach either of those.
// ?native=1 tells the web app it's running inside this native wrapper
// (not a regular Safari tab), so it hides its own phone-mockup chrome —
// the real device already has a notch and status bar of its own.
private let sproutURL = URL(string: "https://sprout-app-hiyu.onrender.com/?native=1")!

struct ContentView: View {
    var body: some View {
        SproutWebView(url: sproutURL)
            .ignoresSafeArea()
    }
}

struct SproutWebView: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default() // keeps you signed in between launches
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.bounces = false
        webView.isOpaque = false
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator() }

    class Coordinator: NSObject, WKNavigationDelegate {
        // Keep external links (if any are ever added) from hijacking the
        // in-app view; everything on the sproutURL host stays inline.
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            decisionHandler(.allow)
        }
    }
}
