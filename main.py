@app.get("/product-variants/{template_id}")
def product_variants(template_id: int):
    try:
        template = odoo_execute(
            "product.template",
            "read",
            [[template_id]],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "product_variant_ids",
                    "attribute_line_ids"
                ]
            }
        )

        if not template:
            return {"status": "not_found"}

        template = template[0]
        variant_ids = template["product_variant_ids"]

        variants = odoo_execute(
            "product.product",
            "read",
            [variant_ids],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "barcode",
                    "lst_price",
                    "product_tmpl_id",
                    "product_template_attribute_value_ids"
                ]
            }
        )

        for variant in variants:

            value_ids = variant["product_template_attribute_value_ids"]

            values = odoo_execute(
                "product.template.attribute.value",
                "read",
                [value_ids],
                {
                    "fields": [
                        "id",
                        "name"
                    ]
                }
            )

            variant["options"] = [
                v["name"] for v in values
            ]

        return {
            "status": "ok",
            "template": template,
            "variant_count": len(variants),
            "variants": variants
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }
