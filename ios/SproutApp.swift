import SwiftUI

// Paste this file's contents into the App.swift that Xcode generates
// when you create the project (File > New > Project > iOS > App).
@main
struct SproutApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                .ignoresSafeArea()
                .preferredColorScheme(nil) // follows system Light/Dark, matching the web app's Auto theme
        }
    }
}
