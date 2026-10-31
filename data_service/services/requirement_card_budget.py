"""Keep each delivered reply within the shared three-card UI budget."""
import re


def limit_requirement_cards(plan):
    confirmations = plan.get('confirmations', [])
    requests = plan.get('input_requests', [])
    environments = [card for card in confirmations if card.get('kind') == 'environment']
    def is_time(card):
        return card.get('target') == 'scope' and (card.get('widget') == 'time-window' or re.search(r'时间|日期|时段', card.get('title', '')))
    if environments:
        # The environment card already owns boundary adoption/customization.
        requests = [card for card in requests if not (card.get('target') == 'scope' and not is_time(card)
                    and re.search(r'区域|范围|边界|地图|地点', card.get('title', '')))]
    # Reserve room for the first location candidate and its independent time
    # question before optional suggestions or further environment candidates.
    priority = environments[:1] + [card for card in requests if is_time(card)][:1]
    ordered = priority + confirmations + requests
    selected = []
    for card in ordered:
        if not any(card is chosen for chosen in selected):
            selected.append(card)
        if len(selected) == 3:
            break
    plan['confirmations'] = [card for card in confirmations if any(card is chosen for chosen in selected)]
    plan['input_requests'] = [card for card in requests if any(card is chosen for chosen in selected)]
    return plan
