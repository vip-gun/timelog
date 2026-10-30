def test_guide_requires_login(browser):
    r = browser().get("/guide", follow_redirects=False)
    assert r.status_code in (302, 303)


def test_guide_for_normal_employee(browser, users):
    b = browser("alice")
    page = b.get("/guide")
    assert page.status_code == 200
    assert "Declaring overtime" in page.text and "Log overtime" in page.text
    assert "Friday at 18:00" in page.text
    # the tab sits in the nav, after the other items and before the Admin link
    home = b.get("/").text
    assert '<a href="/guide"' in home and "User Guide" in home


def test_guide_for_expert_has_no_overtime_howto(browser, users):
    page = browser("bob").get("/guide")
    assert page.status_code == 200
    assert "Experts do not declare overtime" in page.text
    assert "Log overtime" not in page.text


def test_guide_tab_is_just_before_admin(browser, users):
    html = browser("head").get("/").text
    assert "User Guide" in html
    admin = browser()
    assert admin.login("admin").status_code == 303
    html = admin.get("/").text
    assert html.index('href="/guide"') < html.index('class="admin-link')
