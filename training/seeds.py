"""Hand-written seed sentences for the sentinel's two tasks.

TRAIN seeds are paraphrased by a local model to build the training set.
TEST seeds are written separately, in different words and situations, and never
paraphrased into training data, so the evaluation measures generalisation rather
than memorised templates.

Task A (client_signals): labels any of loss_averse, dependent_funds, short_horizon,
life_change; [] if none.
Task B (advisor_claims): labels any of guarantee, deposit_confusion, exaggeration,
pressure; [] if none.
"""

A_TRAIN: dict[str, list[str]] = {
    "loss_averse": [
        "I really can't stomach losing any of this money.",
        "Whatever happens I don't want to see my balance go down.",
        "Losing even a little would keep me up at night.",
        "I need something where the amount never drops.",
        "Please, nothing that can lose value.",
        "If it goes below what I put in, I'll panic.",
        "我一点都不想亏钱。",
        "本金绝对不能少。",
        "I'd rather earn nothing than risk a loss.",
        "My wife will kill me if we lose any of it.",
    ],
    "dependent_funds": [
        "This is everything I've saved over thirty years.",
        "It's the money I'm going to live on after I stop working.",
        "These are my pension payouts, I rely on them.",
        "It's my emergency fund, the only cushion we have.",
        "That's the money for my kid's surgery.",
        "This is my retirement nest egg.",
        "这是我全部的积蓄。",
        "这笔钱是给我养老用的。",
        "We have no other savings besides this.",
        "It's the inheritance my mother left me, it's all we have.",
    ],
    "short_horizon": [
        "I'll need this back in about four months for the wedding.",
        "We're buying a flat next spring and this is the deposit.",
        "My son's tuition is due in September.",
        "I have to pay back a loan with it at the end of the year.",
        "This has to be available in a few weeks.",
        "I'm moving abroad in six months and need it then.",
        "这钱三个月后要付首付。",
        "下学期的学费就靠这笔钱了。",
        "Can I get it out quickly? I'll need it soon.",
        "It's for my car purchase in two months.",
    ],
    "life_change": [
        "The company let me go last month.",
        "I've just been made redundant.",
        "My husband passed away recently and I'm handling the money now.",
        "I was diagnosed with a serious illness.",
        "We're in the middle of a divorce.",
        "My business went under this year.",
        "我上个月失业了。",
        "最近家里有人住院，开销很大。",
        "I'm on unpaid leave until further notice.",
        "My income dropped by half since the restructuring.",
    ],
    "none": [
        "I'd like a balanced mix for the next five years.",
        "I'm comfortable with some ups and downs.",
        "What are your fees on the bond fund?",
        "I've invested in index funds for a decade.",
        "Can you explain what R3 means?",
        "My friend lost his job, but I'm doing fine.",
        "I don't mind short-term losses if the long run is good.",
        "我想了解一下基金的费率。",
        "I want growth, my horizon is ten years.",
        "Is the money market fund liquid?",
    ],
}

B_TRAIN: dict[str, list[str]] = {
    "guarantee": [
        "You'll definitely get your money back with this one.",
        "There's no way you come out behind.",
        "Your capital is completely safe here, I promise.",
        "Think of it as a sure 5% every year.",
        "This product has never lost and it never will.",
        "You can count on getting at least what you put in.",
        "这款产品稳赚不赔。",
        "放心，本金肯定不会少。",
        "It's a locked-in return, no surprises.",
        "Worst case you just break even.",
    ],
    "deposit_confusion": [
        "It works exactly like a savings account, just with better interest.",
        "It's basically a fixed deposit with a higher rate.",
        "Your money is insured the same way as in the bank.",
        "Treat it as cash in the bank.",
        "It's a deposit product, so there's nothing to worry about.",
        "这个理财就跟存款一样。",
        "就当是定期存款，利息更高。",
        "Same protection as your current account.",
        "It's covered by deposit insurance like any deposit.",
        "Just like putting it in a time deposit.",
    ],
    "exaggeration": [
        "This fund only goes up.",
        "Everyone who bought it last year doubled their money.",
        "It's the best-performing product on the market, period.",
        "You can't go wrong with this one.",
        "It's a sure thing, the trend is unstoppable.",
        "Returns like this are basically certain to continue.",
        "这只基金只涨不跌。",
        "买了的人都赚翻了。",
        "Easily 20% a year, every year.",
        "It will beat the market, no question.",
    ],
    "pressure": [
        "You need to decide today, the quota is almost gone.",
        "If you wait, you'll miss out completely.",
        "Sign now, you can read the documents later.",
        "Don't overthink it, just trust me and buy.",
        "This offer disappears at midnight.",
        "今天不买明天就没了。",
        "别犹豫了，先买了再说。",
        "Skip the risk questions, they're just a formality.",
        "Everyone's buying, don't be the one left behind.",
        "I can only hold this price for you for an hour.",
    ],
    "none": [
        "Returns are not guaranteed and you could lose part of your principal.",
        "This is a wealth product, not a deposit, and it isn't covered by deposit insurance.",
        "Past performance doesn't indicate future results.",
        "Take your time, there's a cooling-off period.",
        "The fund returned 6.3% in 2025 according to its fact sheet.",
        "该产品不保本，可能亏损本金。",
        "I recommend reading the risk disclosure before deciding.",
        "This fits your C3 risk level.",
        "Fees are 0.6% a year.",
        "You can redeem within two business days.",
    ],
}

A_TEST: dict[str, list[str]] = {
    "loss_averse": [
        "Seeing red numbers on my account would make me sick.",
        "I won't accept a single yuan of loss.",
        "Keep it safe above all else, I hate losing.",
        "宁可不赚也不能亏。",
    ],
    "dependent_funds": [
        "It's the compensation I got after my accident and I live on it.",
        "This is my whole retirement, there's no plan B.",
        "我和老伴就靠这点钱生活。",
        "Every cent here is earmarked for our old age.",
    ],
    "short_horizon": [
        "I'll be withdrawing it for the renovation in early summer.",
        "We need it for the baby's arrival in a few months.",
        "年底就要拿出来还房贷。",
        "Hold it only until my visa fees are due next quarter.",
    ],
    "life_change": [
        "My contract wasn't renewed, so I'm between jobs.",
        "I've been off work sick since August.",
        "公司倒闭了，我现在没有收入。",
        "We just separated and I'm on my own now.",
    ],
    "none": [
        "I want exposure to Asian bonds this year.",
        "Please compare the fees of your two dividend funds.",
        "Volatility doesn't scare me, I've been through 2015.",
        "我对风险有心理准备，长期持有。",
    ],
}

B_TEST: dict[str, list[str]] = {
    "guarantee": [
        "Rest assured, the bank stands behind every yuan you put in.",
        "You'll be in the black no matter what the market does.",
        "收益是有保障的，不会让您亏。",
        "Principal is protected under any scenario.",
    ],
    "deposit_confusion": [
        "From your point of view it's no different from a term deposit.",
        "It sits in the bank just like your savings, so it's equally safe.",
        "这和您在银行存钱是一回事。",
        "Think of it as a deposit that pays more.",
    ],
    "exaggeration": [
        "This one has nowhere to go but up.",
        "Anyone who held it for a year made a fortune.",
        "闭着眼睛买都能赚。",
        "Nothing on the market comes close to these returns.",
    ],
    "pressure": [
        "Only three slots left, I'd lock one in right now.",
        "Let's just tick the boxes quickly and get you in today.",
        "机会难得，现在不下单就晚了。",
        "Don't bother with the disclosure pages, I'll summarise.",
    ],
    "none": [
        "If markets fall, the value of this fund can fall too.",
        "It's not a deposit; the bank doesn't guarantee its value.",
        "您可以在冷静期内撤销。",
        "Here are the 2023 to 2025 returns from the catalogue, which aren't predictive.",
    ],
}
