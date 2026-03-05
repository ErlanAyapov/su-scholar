from django.core.management.base import BaseCommand

from account.models import User


FEMALE_NAMES = [
    "Абдуллаева Асель Сейдуллаевна",
    "Айтбекова Мадина Берековна",
    "Айтхожаева Евгения Жамалхановна",
    "Алимсеитова Жулдыз Кенесхановна",
    "Амантай Торгын",
    "Аристомбаева Меруерт Турлұбекқызы",
    "Ахмеди Гульдана",
    "Ахмедьярова Айнур Танатаровна",
    "Байматаева Шолпан Муратовна",
    "Бектұрсын Сара Серікқызы",
    "Болатова Тогжан Асетовна",
    "Болысханова Мадина Жакияевна",
    "Гаппар Рабига",
    "Ергалиева Назгуль Оралбековна",
    "Инкарбаева Эльмира Курманкызы",
    "Кабидуллиева Гульдана Ж.",
    "Канат Акбота",
    "Карашина Томирис Утешовна",
    "Кашаганова Гульжан Бакитовна",
    "Кожамжарова Динара Ханатовна",
    "Кошкинбаева Ботагоз Ермекқызы",
    "Мамырова Айша Куанышовна",
    "Молдагулова Айман Николаевна",
    "Мұсабай Дариға",
    "Наурызбаева Аршын Изтлеуовна",
    "Олжабаева Алия Балгабаевна",
    "Рахметулаева Сабина Батырхановна",
    "Сатыбалдиева Рысхан Жакановна",
    "Сейсенбиева Жанна Сарсеналиевна",
    "Сұлтан Жанель Болаткызы",
    "Тулегенова Бакыт Ашимовна",
    "Уралова Фатима Сырлыбай кизи",
    "Шакенова Анаргуль Ахметхановна",
    "Шарипова Гульнар",
    "Юбузова Халича Ибрагимовна",
    "Ягалиева Багдат Есеновна",
    "Қыдырова Жанар Сайлаубековна",
    "Айгерим новый менеджер",
]

MALE_NAMES = [
    "Албанбай Нуртай",
    "Алижан Адиль",
    "Ашимов Абдыгаппар Ашимович",
    "Бексултан Еламан Мұхтарұлы",
    "Болат Алмамбет Тәттімбетович",
    "Досыбаев Максат Маратулы",
    "Әбілмәжінов Расул Айдынұлы",
    "Жумагалиев Биржан Изимович",
    "Кабдуллин Азат Амангельдыулы",
    "Кайрбеков Абылай Муратович",
    "Каламан Ерболат Тлеуханулы",
    "Мағазов Райымбек Саламатұлы",
    "Мукашев Канат",
    "Мұратбекұлы Бекет",
    "Оған Аткелді",
    "Разак Абдул",
    "Сербин Василий Валерьевич",
    "Шукаев Дулат Нурмашевич",
    "Абжанов Куанышбек Кайратович",
    "Акатаев Нурбол Нуртасович",
    "Ахметшәріпов Дәурен Салтанатұлы",
    "Бакпокпаев Азамат Алгузурович",
    "Батыргалиев Асхат Болатханович",
    "Ибраев Дияр Аймуратович",
    "Казиев Галым Зурханаевич",
    "Куникеев Айдын Даулетович",
    "Майкотов Мухит Нурдаулетович",
    "Майлыбаев Ерсайын Курманбайұлы",
    "Олейник Алексей Павлович",
    "Омаров Габит Серикович",
    "Орумбаев Нурсултан Ерболұлы",
    "Саидов Дамир Фуркатович",
    "Самат Ілияс",
    "Турсынбек Ерлан Нуржанулы",
    "Чинибаев Ерсаин Гулисламович",
]


def split_name(full_name: str) -> tuple[str, str, str]:
    parts = full_name.split()
    last_name = parts[0] if parts else ""
    first_name = parts[1] if len(parts) > 1 else ""
    father_name = " ".join(parts[2:]) if len(parts) > 2 else ""
    return last_name, first_name, father_name


class Command(BaseCommand):
    help = "Seed inactive users grouped by female/male lists with is_user=False."

    def handle(self, *args, **options):
        created_count = 0
        updated_count = 0

        for idx, full_name in enumerate(FEMALE_NAMES, start=1):
            username = f"female_{idx:03d}"
            last_name, first_name, father_name = split_name(full_name)
            user, created = User.objects.update_or_create(
                username=username,
                defaults={
                    "last_name": last_name,
                    "first_name": first_name,
                    "father_name": father_name,
                    "gender": "female",
                    "is_user": False,
                    "is_active": False,
                    "is_staff": False,
                    "is_superuser": False,
                },
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])

            if created:
                created_count += 1
            else:
                updated_count += 1

        for idx, full_name in enumerate(MALE_NAMES, start=1):
            username = f"male_{idx:03d}"
            last_name, first_name, father_name = split_name(full_name)
            user, created = User.objects.update_or_create(
                username=username,
                defaults={
                    "last_name": last_name,
                    "first_name": first_name,
                    "father_name": father_name,
                    "gender": "male",
                    "is_user": False,
                    "is_active": False,
                    "is_staff": False,
                    "is_superuser": False,
                },
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])

            if created:
                created_count += 1
            else:
                updated_count += 1

        self.stdout.write(self.style.SUCCESS(f"Created: {created_count}"))
        self.stdout.write(self.style.SUCCESS(f"Updated: {updated_count}"))
        self.stdout.write(self.style.SUCCESS(f"Total users: {User.objects.count()}"))
        self.stdout.write(
            self.style.SUCCESS(f"is_user=False: {User.objects.filter(is_user=False).count()}")
        )
        self.stdout.write(
            self.style.SUCCESS(f"Female: {User.objects.filter(gender='female').count()}")
        )
        self.stdout.write(
            self.style.SUCCESS(f"Male: {User.objects.filter(gender='male').count()}")
        )
