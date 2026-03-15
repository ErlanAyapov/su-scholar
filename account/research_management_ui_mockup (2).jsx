export default function ResearchManagementUIMockup() {

  const kpis = [
    { label: "Публикации", value: "128", sub: "+14%" },
    { label: "Цитирования", value: "2 436", sub: "+9%" },
    { label: "Гранты", value: "18", sub: "+3" },
    { label: "Финансирование", value: "₸ 486 млн", sub: "+11%" },
  ];

  const collaborators = [
    { name: "А. А. Иванов", dept: "Кибербезопасность", pubs: 6, projects: 1 },
    { name: "М. К. Садыкова", dept: "Автоматизация", pubs: 4, projects: 2 },
    { name: "Е. Т. Нургалиев", dept: "Робототехника", pubs: 3, projects: 1 },
  ];

  const quickLinks = [
    "Исследователи",
    "Проекты и гранты",
    "Публикации",
    "Аналитика",
  ];

    const news = [
    "Новая публикация по federated learning для IoT устройств",
    "Открыт набор в исследовательский проект по кибербезопасности",
    "Получен грант на развитие AI и Smart Systems",
  ];

  const workResults = [
    {
      title: "Secure LoRaWAN Architecture for Smart Campus",
      meta: "2024 · Journal Article · DOI: 10.0000/example-001",
      authors: "Н. Албанбай, М. Алимова, А. Жунусов",
    },
    {
      title: "Federated Learning on Resource-Constrained IoT Devices",
      meta: "2024 · Conference Paper · DOI: 10.0000/example-002",
      authors: "Н. Албанбай, Е. Т. Нургалиев",
    },
    {
      title: "Energy Harvesting for Autonomous Sensor Nodes",
      meta: "2023 · Journal Article · DOI: 10.0000/example-003",
      authors: "Н. Албанбай, М. К. Садыкова",
    },
  ];

  const deptRows = [
    ["Кибербезопасность", "46", "812", "7", "₸ 118 млн"],
    ["Автоматизация", "38", "654", "5", "₸ 92 млн"],
    ["Информационные системы", "31", "521", "4", "₸ 76 млн"],
    ["Электроника", "24", "280", "2", "₸ 44 млн"],
  ];

  const instituteRows = [
    ["ИАиИТ", "139", "2 843", "16", "₸ 330 млн"],
    ["Горно-металлургический", "97", "1 506", "11", "₸ 248 млн"],
    ["Энергетика и машиностроение", "82", "1 133", "8", "₸ 194 млн"],
  ];

  const years = [2020, 2021, 2022, 2023, 2024];
  const publications = [42, 51, 58, 71, 84];
  const funding = [72, 95, 110, 132, 158];

  const MiniBars = ({ values, max }) => (
    <div className="flex items-end gap-2 h-28">
      {values.map((v, i) => (
        <div key={i} className="flex-1 flex flex-col items-center gap-2">
          <div
            className="w-full rounded-2xl bg-slate-900/90"
            style={{ height: `${(v / max) * 100}%` }}
          />
          <span className="text-xs text-slate-500">{years[i]}</span>
        </div>
      ))}
    </div>
  );

  const KpiCard = ({ label, value, sub }) => (
    <div className="rounded-3xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
      <div className="text-sm text-slate-500">{label}</div>
      <div className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">{value}</div>
      <div className="mt-2 text-sm text-slate-600">{sub} к прошлому периоду</div>
    </div>
  );

  const SectionTitle = ({ title, subtitle }) => (
    <div className="mb-4">
      <h2 className="text-xl font-semibold text-slate-900">{title}</h2>
      <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
    </div>
  );

  return (
    <div className="min-h-screen bg-slate-100 p-6 text-slate-900">
      <div className="mx-auto max-w-7xl overflow-hidden rounded-[28px] bg-white shadow-xl ring-1 ring-slate-200">
        <main className="bg-slate-50 p-6 lg:p-8">
            <div className="space-y-10">
              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <div className="mb-6 flex flex-col gap-5 xl:flex-row xl:items-center xl:justify-between">
                  <div className="flex items-center gap-4">
                    <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-slate-200 bg-white text-xl font-semibold text-slate-900 shadow-sm">
                      SU
                    </div>
                    <div>
                      <div className="text-[30px] font-semibold leading-none text-slate-900">SU Science</div>
                      <div className="mt-1 text-base text-slate-500">Research Management System</div>
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-3 rounded-[28px] border border-slate-200 bg-white px-3 py-3 shadow-sm">
                    {quickLinks.map((item) => (
                      <div
                        key={item}
                        className="rounded-[22px] border border-slate-200 bg-slate-50 px-5 py-3 text-center text-sm font-medium text-slate-700 transition hover:bg-slate-100"
                      >
                        {item}
                      </div>
                    ))}
                    <div className="rounded-[22px] bg-slate-900 px-5 py-3 text-center text-sm font-medium text-white shadow-sm">
                      Аккаунт
                    </div>
                  </div>
                </div>

                <div className="space-y-6">
                  <div className="overflow-hidden rounded-[28px] ring-1 ring-slate-200">
                    <div className="relative h-[360px] bg-slate-200">
                      <div className="absolute inset-0 bg-[linear-gradient(to_top,rgba(15,23,42,0.72),rgba(15,23,42,0.12))]" />

                      <div className="absolute left-1/2 top-8 w-[min(92%,760px)] -translate-x-1/2 rounded-[26px] border border-white/40 bg-white/90 p-4 shadow-lg backdrop-blur">
                        <div className="flex flex-col gap-3 md:flex-row md:items-center">
                          <div className="flex-1 rounded-[20px] border border-slate-200 bg-white px-5 py-4 text-sm text-slate-400 shadow-sm">
                            Smart іздеу: публикация, автор, DOI, проект, грант
                          </div>
                          <div className="flex gap-3">
                            <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600 shadow-sm">
                              Тип
                            </div>
                            <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600 shadow-sm">
                              Год
                            </div>
                            <button className="rounded-[20px] bg-slate-900 px-6 py-4 text-sm font-medium text-white shadow-sm">
                              Іздеу
                            </button>
                          </div>
                        </div>
                      </div>

                      <div className="absolute bottom-8 left-8 max-w-3xl text-white">
                        <h2 className="text-3xl font-semibold tracking-tight md:text-4xl">
                          Научно-исследовательская экосистема университета
                        </h2>
                        <p className="mt-3 text-sm text-white/85 md:text-base">
                          Профили исследователей, лаборатории, публикации, проекты, гранты и аналитические dashboard в одной системе.
                        </p>
                        <div className="mt-5 flex flex-wrap gap-3">
                          <button className="rounded-2xl bg-white px-5 py-3 text-sm font-medium text-slate-900">Смотреть исследователей</button>
                          <button className="rounded-2xl border border-white/40 px-5 py-3 text-sm font-medium text-white">Открыть аналитику</button>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
                    <div className="space-y-6">
                      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                        <KpiCard label="Исследователи" value="426" sub="активные профили" />
                        <KpiCard label="Публикации" value="418" sub="за текущий период" />
                        <KpiCard label="Гранты" value="52" sub="активные проекты" />
                        <KpiCard label="Финансирование" value="₸ 1.24 млрд" sub="общий объем" />
                      </div>

                      <div className="rounded-3xl border border-slate-200 bg-white p-5">
                        <div className="mb-4 flex items-center justify-between gap-4">
                          <h3 className="text-lg font-semibold">Последние публикации</h3>
                          <span className="rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-600">Все публикации</span>
                        </div>
                        <div className="space-y-3">
                          {[
                            "Energy Harvesting for Autonomous IoT Sensors",
                            "Secure LoRaWAN Architecture for Smart Campus",
                            "Federated Learning on Resource-Constrained Devices",
                          ].map((title, idx) => (
                            <div key={title} className="rounded-2xl bg-slate-50 p-4 ring-1 ring-slate-200">
                              <div className="font-medium text-slate-900">{idx + 1}. {title}</div>
                              <div className="mt-1 text-sm text-slate-500">2024 · Article · DOI · 2 автора из университета</div>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>

                    <div className="space-y-6">
                      <div className="rounded-3xl border border-slate-200 bg-white p-5">
                        <h3 className="text-lg font-semibold">Новости и объявления</h3>
                        <div className="mt-4 space-y-3 text-sm">
                          {news.map((item) => (
                            <div key={item} className="rounded-2xl bg-slate-50 px-4 py-3 ring-1 ring-slate-200">
                              {item}
                            </div>
                          ))}
                        </div>
                      </div>

                      <div className="rounded-3xl border border-slate-200 bg-white p-5">
                        <h3 className="text-lg font-semibold">ИИ ассистент</h3>
                        <p className="mt-2 text-sm text-slate-500">
                          Пользователь может задавать вопросы по публикациям, исследователям, грантам и аналитике системы.
                        </p>
                        <div className="mt-4 rounded-[22px] bg-slate-50 p-4 ring-1 ring-slate-200">
                          <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-400">
                            Например: кто больше всего публиковался в 2024 году?
                          </div>
                          <div className="mt-3 flex flex-wrap gap-2 text-xs">
                            {[
                              "Покажи топ кафедры по грантам",
                              "Найди исследователей по IoT",
                              "Сколько Q1 публикаций у университета?",
                            ].map((item) => (
                              <span key={item} className="rounded-full bg-white px-3 py-2 text-slate-600 ring-1 ring-slate-200">
                                {item}
                              </span>
                            ))}
                          </div>
                          <div className="mt-4 flex justify-end">
                            <button className="rounded-2xl bg-blue-600 px-4 py-2 text-sm font-medium text-white">Задать вопрос</button>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </section>

              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <SectionTitle
                  title="1. Поиск работ"
                  subtitle="Страница поиска публикаций, статей, материалов конференций и других научных результатов"
                />

                <div className="space-y-6">
                  <div className="mx-auto max-w-4xl rounded-[28px] bg-slate-50 p-3 ring-1 ring-slate-200">
                    <div className="grid grid-cols-2 overflow-hidden rounded-[22px] bg-white ring-1 ring-slate-200">
                      <div className="bg-slate-900 px-6 py-4 text-center text-base font-semibold text-white">ЖҰМЫСТАР</div>
                      <div className="px-6 py-4 text-center text-base font-semibold text-slate-400">АДАМДАР</div>
                    </div>
                  </div>

                  <div className="rounded-[28px] border border-slate-200 bg-white p-6">
                    <h3 className="text-2xl font-semibold text-slate-900">Құжаттар іздеуі</h3>
                    <p className="mt-2 text-sm text-slate-500">Іздеу тақырып, автор, DOI, журнал, кілт сөздер бойынша орындалады.</p>

                    <div className="mt-6 grid gap-4 lg:grid-cols-2">
                      <div className="lg:col-span-2">
                        <label className="mb-2 block text-sm font-medium text-slate-600">Іздеу</label>
                        <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4 text-slate-400">Мысалы: iot</div>
                      </div>

                      <div>
                        <label className="mb-2 block text-sm font-medium text-slate-600">Түрі</label>
                        <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">Барлығы</div>
                      </div>
                      <div>
                        <label className="mb-2 block text-sm font-medium text-slate-600">Журнал / конференция</label>
                        <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">Барлығы</div>
                      </div>

                      <div>
                        <label className="mb-2 block text-sm font-medium text-slate-600">Жыл (бастап)</label>
                        <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">1974</div>
                      </div>
                      <div>
                        <label className="mb-2 block text-sm font-medium text-slate-600">Жыл (дейін)</label>
                        <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">2026</div>
                      </div>
                    </div>

                    <div className="mt-5 rounded-2xl border border-dashed border-slate-300 bg-slate-50 px-4 py-4 text-sm text-slate-500">
                      Қосымша параметрлер: бөлімше, автор, ORCID, Open Access, Quartile, Scopus/WoS индексациясы.
                    </div>

                    <div className="mt-6 flex flex-wrap items-center justify-end gap-3">
                      <button className="rounded-2xl border border-slate-300 bg-white px-5 py-3 text-sm font-medium text-slate-700">Тазарту</button>
                      <button className="rounded-2xl bg-blue-600 px-5 py-3 text-sm font-medium text-white">Іздеу</button>
                    </div>
                  </div>

                  <div className="grid gap-6 lg:grid-cols-[0.9fr_1.1fr]">
                    <div className="rounded-[28px] border border-slate-200 bg-white p-6">
                      <h3 className="text-lg font-semibold">Пустое состояние</h3>
                      <div className="mt-6 rounded-[24px] bg-slate-50 p-8 text-center ring-1 ring-slate-200">
                        <div className="text-lg font-semibold text-slate-900">Құжат іздеу шарттарын енгізіңіз</div>
                        <div className="mt-2 text-sm text-slate-500">Автор енгізсеңіз, сол автордың жұмыстары да көрсетіледі.</div>
                      </div>
                    </div>

                    <div className="rounded-[28px] border border-slate-200 bg-white p-6">
                      <div className="mb-4 flex items-center justify-between gap-3">
                        <h3 className="text-lg font-semibold">Пример результатов</h3>
                        <span className="rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-600">Найдено: 128</span>
                      </div>
                      <div className="space-y-3">
                        {workResults.map((item, idx) => (
                          <div key={item.title} className="rounded-2xl bg-slate-50 p-4 ring-1 ring-slate-200">
                            <div className="flex items-start justify-between gap-4">
                              <div>
                                <div className="font-medium text-slate-900">{idx + 1}. {item.title}</div>
                                <div className="mt-1 text-sm text-slate-500">{item.meta}</div>
                                <div className="mt-2 text-sm text-slate-600">{item.authors}</div>
                              </div>
                              <span className="rounded-full bg-white px-3 py-1 text-xs ring-1 ring-slate-200">Открыть</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              </section>

              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <SectionTitle
                  title="2. Профиль исследователя"
                  subtitle="Карточка исследователя с научными результатами, KPI и коллаборациями"
                />

                <div className="grid gap-6 lg:grid-cols-12">
                  <div className="lg:col-span-8 space-y-6">
                    <div className="rounded-3xl border border-slate-200 bg-slate-50 p-5">
                      <div className="flex flex-col gap-5 md:flex-row md:items-start">
                        <div className="h-28 w-28 shrink-0 rounded-3xl bg-slate-200" />
                        <div className="flex-1">
                          <div className="flex flex-wrap items-center gap-3">
                            <h3 className="text-2xl font-semibold">Нуртай Албанбай</h3>
                            <span className="rounded-full bg-slate-900 px-3 py-1 text-xs font-medium text-white">Профессор</span>
                          </div>
                          <p className="mt-2 text-sm text-slate-600">
                            Институт автоматики и информационных технологий · Кафедра кибербезопасности
                          </p>
                          <div className="mt-4 flex flex-wrap gap-2 text-sm">
                            {[
                              "ORCID: 0000-0000-0000-0000",
                              "Scopus Author ID",
                              "Web of Science ID",
                              "Google Scholar",
                            ].map((chip) => (
                              <span key={chip} className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-slate-700">
                                {chip}
                              </span>
                            ))}
                          </div>
                          <div className="mt-4 flex flex-wrap gap-2">
                            {["IoT", "LoRaWAN", "Federated Learning", "Cybersecurity", "Energy Harvesting"].map((chip) => (
                              <span key={chip} className="rounded-full bg-slate-100 px-3 py-1.5 text-sm text-slate-700">
                                {chip}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    </div>

                    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                      {kpis.map((item) => (
                        <KpiCard key={item.label} {...item} />
                      ))}
                    </div>

                    <div className="rounded-3xl border border-slate-200 bg-white p-5">
                      <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                        <h3 className="text-lg font-semibold">Научные результаты</h3>
                        <div className="flex flex-wrap gap-2 text-sm">
                          <div className="rounded-2xl border border-slate-200 px-3 py-2">Поиск публикаций</div>
                          <div className="rounded-2xl border border-slate-200 px-3 py-2">Год</div>
                          <div className="rounded-2xl border border-slate-200 px-3 py-2">Тематика</div>
                          <div className="rounded-2xl border border-slate-200 px-3 py-2">Сортировка</div>
                        </div>
                      </div>
                      <div className="space-y-3">
                        {[
                          "Electromagnetic Vibration Energy Harvester with Magnetic Levitation",
                          "Secure Federated Learning on ESP32 using Lightweight Ciphers",
                          "LoRaWAN Smart-City Deployment and Reliability Analysis",
                          "Energy-Efficient IoT Nodes Powered by Vibration Harvesters",
                        ].map((title, idx) => (
                          <div key={title} className="rounded-2xl bg-slate-50 p-4 ring-1 ring-slate-200">
                            <div className="flex items-start justify-between gap-4">
                              <div>
                                <div className="font-medium text-slate-900">{idx + 1}. {title}</div>
                                <div className="mt-1 text-sm text-slate-500">2024 · Article · Q2 · 12 citations</div>
                              </div>
                              <span className="rounded-full bg-white px-3 py-1 text-xs ring-1 ring-slate-200">Подробнее</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>

                  <div className="lg:col-span-4 space-y-6">
                    <div className="rounded-3xl border border-slate-200 bg-white p-5">
                      <h3 className="text-lg font-semibold">Коллаборации</h3>
                      <div className="mt-3 rounded-3xl bg-slate-50 p-4 ring-1 ring-slate-200">
                        <div className="text-sm text-slate-500">Внутренние</div>
                        <div className="mt-1 text-3xl font-semibold">18</div>
                        <div className="mt-4 h-40 rounded-[22px] border border-dashed border-slate-300 bg-white flex items-center justify-center text-sm text-slate-400">
                          Граф сети коллабораций
                        </div>
                      </div>
                      <div className="mt-4 space-y-3">
                        {collaborators.map((item) => (
                          <div key={item.name} className="rounded-2xl bg-slate-50 p-4 ring-1 ring-slate-200">
                            <div className="font-medium">{item.name}</div>
                            <div className="mt-1 text-sm text-slate-500">{item.dept}</div>
                            <div className="mt-3 flex gap-2 text-xs text-slate-600">
                              <span className="rounded-full bg-white px-2 py-1 ring-1 ring-slate-200">{item.pubs} публикаций</span>
                              <span className="rounded-full bg-white px-2 py-1 ring-1 ring-slate-200">{item.projects} проект</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="rounded-3xl border border-slate-200 bg-white p-5">
                      <h3 className="text-lg font-semibold">Руководству важно</h3>
                      <div className="mt-4 space-y-3 text-sm">
                        {[
                          ["Проекты и гранты", "7"],
                          ["Привлеченное финансирование", "₸ 96 млн"],
                          ["Внешние партнеры", "12"],
                          ["Open Access", "68%"],
                        ].map(([label, value]) => (
                          <div key={label} className="flex items-center justify-between rounded-2xl bg-slate-50 px-4 py-3 ring-1 ring-slate-200">
                            <span className="text-slate-600">{label}</span>
                            <span className="font-semibold text-slate-900">{value}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              </section>

              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <SectionTitle
                  title="3. Dashboard кафедры"
                  subtitle="Понимание, кто дает результат, где рост, а где нужна встряска"
                />
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  <KpiCard label="Исследователи" value="32" sub="2 новых профиля" />
                  <KpiCard label="Публикации" value="46" sub="+12%" />
                  <KpiCard label="Гранты" value="7" sub="+2" />
                  <KpiCard label="Финансирование" value="₸ 118 млн" sub="+15%" />
                </div>
                <div className="mt-6 grid gap-6 lg:grid-cols-2">
                  <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                    <div className="mb-4 text-lg font-semibold">Публикации по годам</div>
                    <MiniBars values={publications} max={90} />
                  </div>
                  <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                    <div className="mb-4 text-lg font-semibold">Финансирование по годам</div>
                    <MiniBars values={funding} max={180} />
                  </div>
                </div>
                <div className="mt-6 rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                  <div className="mb-4 text-lg font-semibold">Топ сотрудников кафедры</div>
                  <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
                    <table className="w-full text-left text-sm">
                      <thead className="bg-slate-100 text-slate-600">
                        <tr>
                          <th className="px-4 py-3">Исследователь</th>
                          <th className="px-4 py-3">Публикации</th>
                          <th className="px-4 py-3">Цитирования</th>
                          <th className="px-4 py-3">Гранты</th>
                          <th className="px-4 py-3">Коллаборации</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[
                          ["Н. Албанбай", "18", "420", "3", "12"],
                          ["М. Алимова", "14", "310", "2", "8"],
                          ["А. Жунусов", "9", "165", "1", "6"],
                        ].map((row, idx) => (
                          <tr key={idx} className="border-t border-slate-200">
                            {row.map((cell) => (
                              <td key={cell} className="px-4 py-3">{cell}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </section>

              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <SectionTitle
                  title="4. Dashboard института"
                  subtitle="Сравнение кафедр, сильные направления и межкафедральные связи"
                />
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  <KpiCard label="Кафедры" value="8" sub="в структуре института" />
                  <KpiCard label="Публикации" value="139" sub="+10%" />
                  <KpiCard label="Гранты" value="16" sub="+4" />
                  <KpiCard label="Финансирование" value="₸ 330 млн" sub="+13%" />
                </div>
                <div className="mt-6 grid gap-6 lg:grid-cols-[1.3fr_1fr]">
                  <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                    <div className="mb-4 text-lg font-semibold">Сравнение кафедр</div>
                    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
                      <table className="w-full text-left text-sm">
                        <thead className="bg-slate-100 text-slate-600">
                          <tr>
                            <th className="px-4 py-3">Кафедра</th>
                            <th className="px-4 py-3">Публикации</th>
                            <th className="px-4 py-3">Цитирования</th>
                            <th className="px-4 py-3">Гранты</th>
                            <th className="px-4 py-3">Финансирование</th>
                          </tr>
                        </thead>
                        <tbody>
                          {deptRows.map((row, idx) => (
                            <tr key={idx} className="border-t border-slate-200">
                              {row.map((cell) => (
                                <td key={cell} className="px-4 py-3">{cell}</td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                  <div className="space-y-6">
                    <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                      <div className="mb-3 text-lg font-semibold">Межкафедральные коллаборации</div>
                      <div className="h-48 rounded-[22px] border border-dashed border-slate-300 bg-white flex items-center justify-center text-sm text-slate-400">
                        Карта связей кафедр института
                      </div>
                    </div>
                    <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                      <div className="mb-3 text-lg font-semibold">Сильные направления</div>
                      <div className="flex flex-wrap gap-2">
                        {[
                          "Cybersecurity",
                          "AI & Data Science",
                          "Industrial IoT",
                          "Automation",
                          "Smart Systems",
                        ].map((item) => (
                          <span key={item} className="rounded-full bg-white px-3 py-2 text-sm ring-1 ring-slate-200">{item}</span>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              </section>

              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <SectionTitle
                  title="5. Dashboard университета"
                  subtitle="Общая картина по науке, качеству публикаций, грантам, рискам и партнерской сети"
                />

                <div className="space-y-6">
                  <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                    <KpiCard label="Институты" value="6" sub="в аналитике" />
                    <KpiCard label="Публикации" value="418" sub="+11%" />
                    <KpiCard label="Финансирование" value="₸ 1.24 млрд" sub="+17%" />
                    <KpiCard label="Международные партнеры" value="57" sub="+8" />
                  </div>

                  <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                    {[
                      ["h-index университета", "63"],
                      ["Q1/Q2 публикации", "61%"],
                      ["Open Access", "68%"],
                      ["Активные исследователи", "426"],
                    ].map(([label, value]) => (
                      <div key={label} className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                        <div className="text-sm text-slate-500">{label}</div>
                        <div className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">{value}</div>
                      </div>
                    ))}
                  </div>

                  <div className="grid gap-6 lg:grid-cols-[1.3fr_1fr]">
                    <div className="space-y-6">
                      <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                        <div className="mb-4 flex items-center justify-between gap-3">
                          <div>
                            <div className="text-lg font-semibold">Сравнение институтов</div>
                            <div className="mt-1 text-sm text-slate-500">Публикации, цитирования, гранты и финансирование</div>
                          </div>
                          <span className="rounded-full bg-white px-3 py-1 text-xs ring-1 ring-slate-200">Рейтинг подразделений</span>
                        </div>
                        <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
                          <table className="w-full text-left text-sm">
                            <thead className="bg-slate-100 text-slate-600">
                              <tr>
                                <th className="px-4 py-3">Институт</th>
                                <th className="px-4 py-3">Публикации</th>
                                <th className="px-4 py-3">Цитирования</th>
                                <th className="px-4 py-3">Гранты</th>
                                <th className="px-4 py-3">Финансирование</th>
                              </tr>
                            </thead>
                            <tbody>
                              {instituteRows.map((row, idx) => (
                                <tr key={idx} className="border-t border-slate-200">
                                  {row.map((cell) => (
                                    <td key={cell} className="px-4 py-3">{cell}</td>
                                  ))}
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>

                      <div className="grid gap-6 lg:grid-cols-2">
                        <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                          <div className="mb-4 text-lg font-semibold">Качество публикаций</div>
                          <div className="space-y-3 text-sm">
                            {[
                              ["Средняя цитируемость на 1 публикацию", "5.8"],
                              ["Публикации в Q1", "28%"],
                              ["Публикации в Q2", "33%"],
                              ["Международные публикации", "44%"],
                            ].map(([label, value]) => (
                              <div key={label} className="flex items-center justify-between rounded-2xl bg-white px-4 py-3 ring-1 ring-slate-200">
                                <span className="text-slate-600">{label}</span>
                                <span className="font-semibold text-slate-900">{value}</span>
                              </div>
                            ))}
                          </div>
                        </div>

                        <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                          <div className="mb-4 text-lg font-semibold">Гранты и коммерциализация</div>
                          <div className="space-y-3 text-sm">
                            {[
                              ["Активные гранты", "52"],
                              ["Успешность заявок", "37%"],
                              ["Патенты и свидетельства", "24"],
                              ["Доход от НИОКР и контрактов", "₸ 214 млн"],
                            ].map(([label, value]) => (
                              <div key={label} className="flex items-center justify-between rounded-2xl bg-white px-4 py-3 ring-1 ring-slate-200">
                                <span className="text-slate-600">{label}</span>
                                <span className="font-semibold text-slate-900">{value}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    </div>

                    <div className="space-y-6">
                      <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                        <div className="mb-3 text-lg font-semibold">Риск-зоны</div>
                        <div className="space-y-3 text-sm">
                          {[
                            "2 кафедры со снижением публикационной динамики",
                            "11 профилей без внешних коллабораций",
                            "Open Access ниже 50% в 1 институте",
                            "3 гранта завершаются в этом квартале",
                            "1 институт с низкой долей Q1/Q2 публикаций",
                          ].map((item) => (
                            <div key={item} className="rounded-2xl bg-white px-4 py-3 ring-1 ring-slate-200">{item}</div>
                          ))}
                        </div>
                      </div>

                      <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                        <div className="mb-3 text-lg font-semibold">Партнерская сеть</div>
                        <div className="h-40 rounded-[22px] border border-dashed border-slate-300 bg-white flex items-center justify-center text-sm text-slate-400">
                          Международные и внутренние коллаборации
                        </div>
                        <div className="mt-4 space-y-3 text-sm">
                          {[
                            ["Внутренние коллаборации", "138"],
                            ["Международные коллаборации", "57"],
                            ["Топ партнер", "University of Malaya"],
                            ["Междисциплинарные проекты", "19"],
                          ].map(([label, value]) => (
                            <div key={label} className="flex items-center justify-between rounded-2xl bg-white px-4 py-3 ring-1 ring-slate-200">
                              <span className="text-slate-600">{label}</span>
                              <span className="font-semibold text-slate-900">{value}</span>
                            </div>
                          ))}
                        </div>
                      </div>

                      <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                        <div className="mb-3 text-lg font-semibold">Управленческие индикаторы</div>
                        <div className="space-y-3 text-sm">
                          {[
                            ["Исследователи без публикаций", "23"],
                            ["Среднее число публикаций на 1 исследователя", "3.4"],
                            ["Среднее финансирование на 1 проект", "₸ 23.8 млн"],
                            ["Защищенные диссертации", "18"],
                          ].map(([label, value]) => (
                            <div key={label} className="flex items-center justify-between rounded-2xl bg-white px-4 py-3 ring-1 ring-slate-200">
                              <span className="text-slate-600">{label}</span>
                              <span className="font-semibold text-slate-900">{value}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </section>
            </div>
          </main>
      </div>
    </div>
  );
}
