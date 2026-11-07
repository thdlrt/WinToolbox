#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <atomic>
#include <chrono>
#include <deque>
#include <functional>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

// ABI reference: official BambuStudio src/slic3r/Utils/NetworkAgent.hpp.
// Load the owner's installed SDK; no proprietary libraries are redistributed.
template<class T> T load(HMODULE module, const char* name) {
    auto address = GetProcAddress(module, name);
    if (!address) throw std::runtime_error("incompatible-sdk");
    return reinterpret_cast<T>(address);
}

std::string utf8(const wchar_t* value) {
    int count = WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
    std::string result(count, '\0');
    WideCharToMultiByte(CP_UTF8, 0, value, -1, result.data(), count, nullptr, nullptr);
    result.pop_back();
    return result;
}

int wmain(int argc, wchar_t** argv) {
    if (argc != 7) return 2; // SDK, private session, certificate folder, country, serial, log folder.
    std::mutex output;
    auto emit = [&](const std::string& json) {
        std::lock_guard<std::mutex> lock(output);
        std::cout << "WT_BAMBU_CLOUD " << json << std::endl;
    };
    auto module = LoadLibraryExW(argv[1], nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!module) { emit("{\"event\":\"error\",\"error\":\"sdk-load\"}"); return 3; }
    try {
        auto create = load<void*(*)(std::string)>(module, "bambu_network_create_agent");
        auto destroy = load<int(*)(void*)>(module, "bambu_network_destroy_agent");
        auto setconfig = load<int(*)(void*, std::string)>(module, "bambu_network_set_config_dir");
        auto setcert = load<int(*)(void*, std::string, std::string)>(module, "bambu_network_set_cert_file");
        auto setcountry = load<int(*)(void*, std::string)>(module, "bambu_network_set_country_code");
        auto countryfn = load<int(*)(void*, std::function<std::string()>)>(module, "bambu_network_set_get_country_code_fn");
        auto queuefn = load<int(*)(void*, std::function<void(std::function<void()>)>)>(module, "bambu_network_set_queue_on_main_fn");
        auto serverfn = load<int(*)(void*, std::function<void(int, int)>)>(module, "bambu_network_set_on_server_connected_fn");
        auto printerfn = load<int(*)(void*, std::function<void(std::string)>)>(module, "bambu_network_set_on_printer_connected_fn");
        auto messagefn = load<int(*)(void*, std::function<void(std::string, std::string)>)>(module, "bambu_network_set_on_message_fn");
        auto start = load<int(*)(void*)>(module, "bambu_network_start");
        auto loggedin = load<bool(*)(void*)>(module, "bambu_network_is_user_login");
        auto connect = load<int(*)(void*)>(module, "bambu_network_connect_server");
        auto add = load<int(*)(void*, std::vector<std::string>)>(module, "bambu_network_add_subscribe");
        auto subscribe = load<int(*)(void*, std::string)>(module, "bambu_network_start_subscribe");
        auto unsubscribe = load<int(*)(void*, std::string)>(module, "bambu_network_stop_subscribe");
        auto send = load<int(*)(void*, std::string, std::string, int, int)>(module, "bambu_network_send_message");
        const auto serial = utf8(argv[5]), country = utf8(argv[4]);
        auto agent = create(utf8(argv[6]));
        if (!agent) { emit("{\"event\":\"error\",\"error\":\"sdk-create\"}"); return 4; }
        std::mutex mutex;
        std::deque<std::function<void()>> jobs;
        auto enqueue = [&](std::function<void()> job) { std::lock_guard<std::mutex> lock(mutex); jobs.push_back(job); };
        std::atomic<bool> stopping = false;
        bool subscribed = false;
        auto last_request = std::chrono::steady_clock::now();
        auto request_status = [&] {
            // Exact read-only pushall request used by official MachineObject::command_request_push_all.
            int code = send(agent, serial, "{\"pushing\":{\"sequence_id\":\"0\",\"command\":\"pushall\",\"version\":1,\"push_target\":1}}", 0, 0);
            last_request = std::chrono::steady_clock::now();
            emit("{\"event\":\"request\",\"code\":" + std::to_string(code) + "}");
        };
        queuefn(agent, enqueue);
        countryfn(agent, [country] { return country; });
        serverfn(agent, [&](int code, int reason) {
            emit("{\"event\":\"server\",\"code\":" + std::to_string(code) + ",\"reason\":" + std::to_string(reason) + "}");
            enqueue([&, code] { subscribed = false; if (code == 0 && !stopping) { add(agent, {serial}); subscribe(agent, "app"); } });
        });
        printerfn(agent, [&](std::string id) { if (id == serial) enqueue([&] { subscribed = true; request_status(); }); });
        messagefn(agent, [&](std::string id, std::string message) {
            if (!stopping && id == serial && message.size() <= 131072)
                emit("{\"event\":\"report\",\"payload\":" + message + "}");
        });
        if (setconfig(agent, utf8(argv[2])) != 0 || setcert(agent, utf8(argv[3]), "slicer_base64.cer") != 0 ||
            setcountry(agent, country) != 0 || start(agent) != 0) {
            emit("{\"event\":\"error\",\"error\":\"sdk-initialize\"}"); destroy(agent); return 5;
        }
        if (!loggedin(agent)) { emit("{\"event\":\"error\",\"error\":\"login-required\"}"); destroy(agent); return 6; }
        emit("{\"event\":\"authenticated\"}");
        int code = connect(agent);
        emit("{\"event\":\"connect\",\"code\":" + std::to_string(code) + "}");
        std::thread input([&] { std::string line; while (std::getline(std::cin, line)) if (line == "quit") break; stopping = true; });
        while (!stopping) {
            std::function<void()> job;
            { std::lock_guard<std::mutex> lock(mutex); if (!jobs.empty()) { job = std::move(jobs.front()); jobs.pop_front(); } }
            if (job) job();
            if (subscribed && std::chrono::steady_clock::now() - last_request > std::chrono::seconds(60)) request_status();
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
        input.join();
        unsubscribe(agent, "app");
        destroy(agent);
    } catch (...) { emit("{\"event\":\"error\",\"error\":\"sdk-call\"}"); return 7; }
    return 0;
}
