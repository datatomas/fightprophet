# 1. Astro pages still good
curl -sI https://fightprophet.com/predictions/ | head -1

# 2. Streamlit canonical injection — open in browser, view source, look for:
#    <link rel="canonical" href="https://fightprophet.com/predictions/">
#    (it's added by JS so check via DevTools → Elements, not raw view-source)
